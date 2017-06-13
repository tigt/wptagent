# Copyright 2017 Google Inc. All rights reserved.
# Use of this source code is governed by the Apache 2.0 license that can be
# found in the LICENSE file.
"""Main entry point for interfacing with Chrome's remote debugging protocol"""
import base64
import gzip
import logging
import os
import Queue
import re
import subprocess
import time
import monotonic
import ujson as json
from ws4py.client.threadedclient import WebSocketClient

class DevTools(object):
    """Interface into Chrome's remote dev tools protocol"""
    def __init__(self, options, job, task, use_devtools_video):
        self.url = "http://localhost:{0:d}/json".format(task['port'])
        self.websocket = None
        self.options = options
        self.job = job
        self.task = task
        self.command_id = 0
        self.page_loaded = None
        self.main_frame = None
        self.is_navigating = False
        self.last_activity = monotonic.monotonic()
        self.dev_tools_file = None
        self.trace_file = None
        self.trace_enabled = False
        self.requests = {}
        self.response_bodies = {}
        self.nav_error = None
        self.main_request = None
        self.start_timestamp = None
        self.path_base = None
        self.support_path = None
        self.video_path = None
        self.video_prefix = None
        self.recording = False
        self.mobile_viewport = None
        self.tab_id = None
        self.use_devtools_video = use_devtools_video
        self.recording_video = False
        self.main_thread_blocked = False
        self.prepare()

    def prepare(self):
        """Set up the various paths and states"""
        self.requests = {}
        self.response_bodies = {}
        self.nav_error = None
        self.main_request = None
        self.start_timestamp = None
        self.path_base = os.path.join(self.task['dir'], self.task['prefix'])
        self.support_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), "support")
        self.video_path = os.path.join(self.task['dir'], self.task['video_subdirectory'])
        self.video_prefix = os.path.join(self.video_path, 'ms_')
        if not os.path.isdir(self.video_path):
            os.makedirs(self.video_path)

    def start_navigating(self):
        """Indicate that we are about to start a known-navigation"""
        self.main_frame = None
        self.is_navigating = True

    def wait_for_available(self, timeout):
        """Wait for the dev tools interface to become available (but don't connect)"""
        import requests
        ret = False
        end_time = monotonic.monotonic() + timeout
        while not ret and monotonic.monotonic() < end_time:
            try:
                response = requests.get(self.url, timeout=timeout)
                if len(response.text):
                    tabs = response.json()
                    logging.debug("Dev Tools tabs: %s", json.dumps(tabs))
                    if len(tabs):
                        for index in xrange(len(tabs)):
                            if 'type' in tabs[index] and \
                                    tabs[index]['type'] == 'page' and \
                                    'webSocketDebuggerUrl' in tabs[index] and \
                                    'id' in tabs[index]:
                                ret = True
                                logging.debug('Dev tools interface is available')
            except Exception as err:
                logging.critical("Connect to dev tools Error: %s", err.__str__())
                time.sleep(0.5)
        return ret

    def connect(self, timeout):
        """Connect to the browser"""
        import requests
        ret = False
        end_time = monotonic.monotonic() + timeout
        while not ret and monotonic.monotonic() < end_time:
            try:
                response = requests.get(self.url, timeout=timeout)
                if len(response.text):
                    tabs = response.json()
                    logging.debug("Dev Tools tabs: %s", json.dumps(tabs))
                    if len(tabs):
                        websocket_url = None
                        for index in xrange(len(tabs)):
                            if 'type' in tabs[index] and \
                                    tabs[index]['type'] == 'page' and \
                                    'webSocketDebuggerUrl' in tabs[index] and \
                                    'id' in tabs[index]:
                                if websocket_url is None:
                                    websocket_url = tabs[index]['webSocketDebuggerUrl']
                                    self.tab_id = tabs[index]['id']
                                else:
                                    # Close extra tabs
                                    requests.get(self.url + '/close/' + tabs[index]['id'])
                        if websocket_url is not None:
                            try:
                                self.websocket = DevToolsClient(websocket_url)
                                self.websocket.connect()
                                ret = True
                            except Exception as err:
                                logging.critical("Connect to dev tools websocket Error: %s",
                                                 err.__str__())
                            if not ret:
                                # try connecting to 127.0.0.1 instead of localhost
                                try:
                                    websocket_url = websocket_url.replace('localhost', '127.0.0.1')
                                    self.websocket = DevToolsClient(websocket_url)
                                    self.websocket.connect()
                                    ret = True
                                except Exception as err:
                                    logging.critical("Connect to dev tools websocket Error: %s",
                                                     err.__str__())
                        else:
                            time.sleep(0.5)
                    else:
                        time.sleep(0.5)
            except Exception as err:
                logging.critical("Connect to dev tools Error: %s", err.__str__())
                time.sleep(0.5)
        return ret

    def close(self, close_tab=True):
        """Close the dev tools connection"""
        if self.websocket:
            try:
                self.websocket.close()
            except Exception:
                pass
            self.websocket = None
        if close_tab and self.tab_id is not None:
            import requests
            requests.get(self.url + '/close/' + self.tab_id)
        self.tab_id = None

    def start_recording(self):
        """Start capturing dev tools, timeline and trace data"""
        self.prepare()
        self.recording = True
        if self.use_devtools_video and self.job['video'] and self.task['log_data']:
            self.grab_screenshot(self.video_prefix + '000000.jpg', png=False)
        elif self.mobile_viewport is None and not self.options.android and \
                'mobile' in self.job and self.job['mobile']:
            # grab an initial screen shot to get the crop rectangle
            try:
                tmp_file = os.path.join(self.task['dir'], 'tmp.png')
                self.grab_screenshot(tmp_file)
                os.remove(tmp_file)
            except Exception:
                pass
        self.flush_pending_messages()
        self.send_command('Page.enable', {})
        self.send_command('Inspector.enable', {})
        self.send_command('Network.enable', {})
        if 'user_agent_string' in self.job:
            self.send_command('Network.setUserAgentOverride',
                              {'userAgent': self.job['user_agent_string']}, wait=True)
        if 'headers' in self.job:
            self.send_command('Network.setExtraHTTPHeaders',
                              {'headers': self.job['headers']}, wait=True)
        if len(self.task['block']):
            for block in self.task['block']:
                self.send_command('Network.addBlockedURL', {'url': block})
            self.send_command('Network.setBlockedURLs', {'urls': self.task['block']})
        if self.task['log_data']:
            self.send_command('Security.enable', {})
            self.send_command('Console.enable', {})
            if 'trace' in self.job and self.job['trace']:
                if 'traceCategories' in self.job:
                    trace = self.job['traceCategories']
                else:
                    trace = "-*,toplevel,blink,v8,cc,gpu,blink.net," \
                            "disabled-by-default-v8.runtime_stats"
            else:
                trace = "-*"
            if 'timeline' in self.job and self.job['timeline']:
                trace += ",blink.console,devtools.timeline" \
                         ",disabled-by-default-blink.feature_usage"
            if self.use_devtools_video and self.job['video']:
                trace += ",disabled-by-default-devtools.screenshot"
                self.recording_video = True
            trace += ",rail,blink.user_timing,netlog"
            self.trace_enabled = True
            self.send_command('Tracing.start',
                              {'categories': trace, 'options': 'record-as-much-as-possible'},
                              wait=True)
        now = monotonic.monotonic()
        if not self.task['stop_at_onload']:
            self.last_activity = now
        if self.page_loaded is not None:
            self.page_loaded = now

    def stop_recording(self):
        """Stop capturing dev tools, timeline and trace data"""
        self.recording = False
        self.send_command('Inspector.disable', {})
        self.send_command('Page.disable', {})
        self.collect_trace()
        self.flush_pending_messages()
        if self.task['log_data']:
            self.send_command('Security.disable', {})
            self.send_command('Console.disable', {})
            self.get_response_bodies()
        self.send_command('Network.disable', {})
        if self.dev_tools_file is not None:
            self.dev_tools_file.write("\n]")
            self.dev_tools_file.close()
            self.dev_tools_file = None

    def collect_trace(self):
        """Stop tracing and collect the results"""
        if self.trace_enabled:
            self.trace_enabled = False
            video_prefix = self.video_prefix if self.recording_video else None
            self.websocket.start_processing_trace(self.path_base, video_prefix,
                                                  self.options, self.job, self.task,
                                                  self.start_timestamp)
            self.send_command('Tracing.end', {})
            start = monotonic.monotonic()
            # Keep pumping messages until we get tracingComplete or
            # we get a gap of 30 seconds between messages
            if self.websocket:
                logging.info('Collecting trace events')
                done = False
                no_message_count = 0
                while not done and no_message_count < 30:
                    try:
                        raw = self.websocket.get_message(1)
                        if raw is not None and len(raw):
                            no_message_count = 0
                            msg = json.loads(raw)
                            if 'method' in msg and msg['method'] == 'Tracing.tracingComplete':
                                done = True
                        else:
                            no_message_count += 1
                    except Exception:
                        pass
            self.websocket.stop_processing_trace()
            elapsed = monotonic.monotonic() - start
            logging.debug("Time to collect trace: %0.3f sec", elapsed)
            self.recording_video = False

    def get_response_bodies(self):
        """Retrieve all of the response bodies for the requests that we know about"""
        import zipfile
        requests = self.get_requests()
        if self.task['error'] is None and requests:
            optimization_checks_disabled = bool('noopt' in self.job and self.job['noopt'])
            # see if we also need to zip them up
            zip_file = None
            if 'bodies' in self.job and self.job['bodies']:
                zip_file = zipfile.ZipFile(self.path_base + '_bodies.zip', 'w',
                                           zipfile.ZIP_DEFLATED)
            path = os.path.join(self.task['dir'], 'bodies')
            if not os.path.isdir(path):
                os.makedirs(path)
            index = 0
            fail_count = 0
            for request_id in requests:
                request = requests[request_id]
                if fail_count < 2 and 'status' in request and request['status'] == 200 and \
                        'response_headers' in request and request_id not in self.response_bodies:
                    content_length = self.get_header_value(request['response_headers'],
                                                           'Content-Length')
                    if content_length is not None:
                        content_length = int(re.search(r'\d+', str(content_length)).group())
                    elif 'transfer_size' in request:
                        content_length = request['transfer_size']
                    else:
                        content_length = 0
                    logging.debug('%s (%d) - %s', request_id, content_length, request['url'])
                    body_file_path = os.path.join(path, request_id)
                    if not os.path.exists(body_file_path):
                        # Only grab bodies needed for optimization checks
                        # or if we are saving full bodies
                        need_body = True
                        content_type = self.get_header_value(request['response_headers'],
                                                             'Content-Type')
                        is_text = False
                        if content_type is not None:
                            content_type = content_type.lower()
                            if content_type[:5] == 'text/' or \
                                    content_type.find('javascript') >= 0 or \
                                    content_type.find('json') >= 0:
                                is_text = True
                            # Ignore video files over 10MB
                            if content_type[:6] == 'video/' and content_length > 10000000:
                                need_body = False
                        if optimization_checks_disabled and zip_file is None:
                            need_body = False
                        if need_body:
                            response = self.send_command("Network.getResponseBody",
                                                         {'requestId': request_id}, wait=True)
                            if response is None:
                                fail_count += 1
                                logging.warning('No response to body request for request %s',
                                                request_id)
                            elif 'result' not in response or \
                                    'body' not in response['result']:
                                fail_count = 0
                                logging.warning('Missing response body for request %s',
                                                request_id)
                            elif len(response['result']['body']):
                                fail_count = 0
                                # Write the raw body to a file (all bodies)
                                if 'base64Encoded' in response['result'] and \
                                        response['result']['base64Encoded']:
                                    body = base64.b64decode(response['result']['body'])
                                else:
                                    body = response['result']['body'].encode('utf-8')
                                    is_text = True
                                # Add text bodies to the zip archive
                                if zip_file is not None and is_text:
                                    index += 1
                                    name = '{0:03d}-{1}-body.txt'.format(index, request_id)
                                    zip_file.writestr(name, body)
                                    logging.debug('Stored body in zip')
                                logging.debug('Body length: %d', len(body))
                                self.response_bodies[request_id] = body
                                with open(body_file_path, 'wb') as body_file:
                                    body_file.write(body)
            if zip_file is not None:
                zip_file.close()

    def get_requests(self):
        """Get a dictionary of all of the requests and the details (headers, body file)"""
        requests = None
        if self.requests:
            body_path = os.path.join(self.task['dir'], 'bodies')
            for request_id in self.requests:
                if 'fromNet' in self.requests[request_id] and self.requests[request_id]['fromNet']:
                    events = self.requests[request_id]
                    request = {'id': request_id}
                    # See if we have a body
                    body_file_path = os.path.join(body_path, request_id)
                    if os.path.isfile(body_file_path):
                        request['body'] = body_file_path
                    if request_id in self.response_bodies:
                        request['response_body'] = self.response_bodies[request_id]
                    # Get the headers from responseReceived
                    if 'response' in events:
                        response = events['response'][-1]
                        if 'response' in response:
                            if 'url' in response['response']:
                                request['url'] = response['response']['url']
                            if 'status' in response['response']:
                                request['status'] = response['response']['status']
                            if 'headers' in response['response']:
                                request['response_headers'] = response['response']['headers']
                            if 'requestHeaders' in response['response']:
                                request['request_headers'] = response['response']['requestHeaders']
                            if 'connectionId' in response['response']:
                                request['connection'] = response['response']['connectionId']
                    # Fill in any missing details from the requestWillBeSent event
                    if 'request' in events:
                        req = events['request'][-1]
                        if 'request' in req:
                            if 'url' not in request and 'url' in req['request']:
                                request['url'] = req['request']['url']
                            if 'request_headers' not in request and 'headers' in req['request']:
                                request['request_headers'] = req['request']['headers']
                    # Get the response length from the data events
                    if 'finished' in events and 'encodedDataLength' in events['finished']:
                        request['transfer_size'] = events['finished']['encodedDataLength']
                    elif 'data' in events:
                        transfer_size = 0
                        for data in events['data']:
                            if 'encodedDataLength' in data:
                                transfer_size += data['encodedDataLength']
                            elif 'dataLength' in data:
                                transfer_size += data['dataLength']
                        request['transfer_size'] = transfer_size

                    if requests is None:
                        requests = {}
                    requests[request_id] = request
        return requests

    def flush_pending_messages(self):
        """Clear out any pending websocket messages"""
        if self.websocket:
            try:
                while True:
                    raw = self.websocket.get_message(0)
                    if raw is not None and len(raw):
                        if self.recording:
                            logging.debug(raw[:200])
                            msg = json.loads(raw)
                            self.process_message(msg)
                    if not raw:
                        break
            except Exception:
                pass

    def send_command(self, method, params, wait=False, timeout=10):
        """Send a raw dev tools message and optionally wait for the response"""
        ret = None
        if self.websocket:
            self.command_id += 1
            msg = {'id': self.command_id, 'method': method, 'params': params}
            try:
                out = json.dumps(msg)
                logging.debug("Sending: %s", out)
                self.websocket.send(out)
                if wait:
                    end_time = monotonic.monotonic() + timeout
                    while ret is None and monotonic.monotonic() < end_time:
                        try:
                            raw = self.websocket.get_message(1)
                            if raw is not None and len(raw):
                                logging.debug(raw[:200])
                                msg = json.loads(raw)
                                self.process_message(msg)
                                if 'id' in msg and \
                                    int(re.search(r'\d+', str(msg['id'])).group()) == \
                                    self.command_id:
                                    ret = msg
                        except Exception:
                            pass
            except Exception as err:
                logging.critical("Websocket send error: %s", err.__str__())
        return ret

    def wait_for_page_load(self):
        """Wait for the page load and activity to finish"""
        if self.websocket:
            start_time = monotonic.monotonic()
            end_time = start_time + self.task['time_limit']
            done = False
            while not done:
                try:
                    raw = self.websocket.get_message(1)
                    if raw is not None and len(raw):
                        logging.debug(raw[:200])
                        msg = json.loads(raw)
                        self.process_message(msg)
                except Exception:
                    # ignore timeouts when we're in a polling read loop
                    pass
                now = monotonic.monotonic()
                elapsed_test = now - start_time
                if self.nav_error is not None:
                    done = True
                    if self.page_loaded is None:
                        self.task['error'] = self.nav_error
                elif now >= end_time:
                    done = True
                    # only consider it an error if we didn't get a page load event
                    if self.page_loaded is None:
                        self.task['error'] = "Page Load Timeout"
                elif 'time' not in self.job or elapsed_test > self.job['time']:
                    elapsed_activity = now - self.last_activity
                    elapsed_page_load = now - self.page_loaded if self.page_loaded else 0
                    if elapsed_page_load >= 1 and elapsed_activity >= self.task['activity_time']:
                        done = True
                    elif self.task['error'] is not None:
                        done = True

    def grab_screenshot(self, path, png=True, resize=0):
        """Save the screen shot (png or jpeg)"""
        if not self.main_thread_blocked:
            response = self.send_command("Page.captureScreenshot", {}, wait=True, timeout=10)
            if response is not None and 'result' in response and 'data' in response['result']:
                resize_string = '' if not resize else '-resize {0:d}x{0:d} '.format(resize)
                if png:
                    with open(path, 'wb') as image_file:
                        image_file.write(base64.b64decode(response['result']['data']))
                    # Fix png issues
                    cmd = 'mogrify -format png -define png:color-type=2 '\
                            '-depth 8 {0}"{1}"'.format(resize_string, path)
                    logging.debug(cmd)
                    subprocess.call(cmd, shell=True)
                    self.crop_screen_shot(path)
                else:
                    tmp_file = path + '.png'
                    with open(tmp_file, 'wb') as image_file:
                        image_file.write(base64.b64decode(response['result']['data']))
                    self.crop_screen_shot(tmp_file)
                    command = 'convert "{0}" {1}-quality {2:d} "{3}"'.format(
                        tmp_file, resize_string, self.job['iq'], path)
                    logging.debug(command)
                    subprocess.call(command, shell=True)
                    if os.path.isfile(tmp_file):
                        try:
                            os.remove(tmp_file)
                        except Exception:
                            pass

    def colors_are_similar(self, color1, color2, threshold=15):
        """See if 2 given pixels are of similar color"""
        similar = True
        delta_sum = 0
        for value in xrange(3):
            delta = abs(color1[value] - color2[value])
            delta_sum += delta
            if delta > threshold:
                similar = False
        if delta_sum > threshold:
            similar = False
        return similar

    def crop_screen_shot(self, path):
        """Crop screenshots to the viewport (for mobile emulation tests)"""
        if not self.options.android and 'mobile' in self.job and self.job['mobile']:
            try:
                # detect the viewport if we haven't already
                if self.mobile_viewport is None:
                    from PIL import Image
                    image = Image.open(path)
                    width, height = image.size
                    if 'width' in self.job and 'height' in self.job and \
                            width >= self.job['width'] and height > self.job['height']:
                        viewport_width = self.job['width']
                        viewport_height = self.job['height']
                    else:
                        pixels = image.load()
                        background = pixels[10, 10]
                        viewport_width = None
                        viewport_height = None
                        x_pos = 10
                        y_pos = 10
                        while viewport_width is None and x_pos < width:
                            pixel_color = pixels[x_pos, y_pos]
                            if not self.colors_are_similar(background, pixel_color):
                                viewport_width = x_pos
                            else:
                                x_pos += 1
                        if viewport_width is None:
                            viewport_width = width
                        x_pos = 10
                        while viewport_height is None and y_pos < height:
                            pixel_color = pixels[x_pos, y_pos]
                            if not self.colors_are_similar(background, pixel_color):
                                viewport_height = y_pos
                            else:
                                y_pos += 1
                        if viewport_height is None:
                            viewport_height = height
                    self.mobile_viewport = '{0:d}x{1:d}+0+0'.format(viewport_width, viewport_height)
                    logging.debug('Mobile viewport found: %s in %dx%d screen shot',
                                  self.mobile_viewport, width, height)
                    if width > 0 and height > 0:
                        if width != viewport_width or height != viewport_height:
                            self.task['crop_pct'] = {
                                "width": int(float(viewport_width * 100) / float(width)),
                                "height": int(float(viewport_height * 100) / float(height)),
                            }
                            logging.debug("Crop percentages: %f%% x %f%%",
                                          self.task['crop_pct']['width'],
                                          self.task['crop_pct']['height'])
                if self.mobile_viewport is not None:
                    command = 'mogrify -crop {0} "{1}"'.format(self.mobile_viewport, path)
                    logging.debug(command)
                    subprocess.call(command, shell=True)
            except Exception:
                pass

    def execute_js(self, script):
        """Run the provided JS in the browser and return the result"""
        ret = None
        if self.task['error'] is None and not self.main_thread_blocked:
            response = self.send_command("Runtime.evaluate",
                                         {'expression': script, 'returnByValue': True},
                                         wait=True, timeout=30)
            if response is not None and 'result' in response and\
                    'result' in response['result'] and\
                    'value' in response['result']['result']:
                ret = response['result']['result']['value']
        return ret

    def process_message(self, msg):
        """Process an inbound dev tools message"""
        if 'method' in msg and self.recording:
            parts = msg['method'].split('.')
            if len(parts) >= 2:
                category = parts[0]
                event = parts[1]
                if category == 'Page':
                    self.process_page_event(event, msg)
                    self.log_dev_tools_event(msg)
                elif category == 'Network':
                    self.process_network_event(event, msg)
                    self.log_dev_tools_event(msg)
                elif category == 'Inspector':
                    self.process_inspector_event(event)
                else:
                    self.log_dev_tools_event(msg)

    def process_page_event(self, event, msg):
        """Process Page.* dev tools events"""
        if event == 'loadEventFired':
            self.page_loaded = monotonic.monotonic()
        elif event == 'frameStartedLoading' and 'params' in msg and 'frameId' in msg['params']:
            if self.is_navigating and self.main_frame is None:
                self.is_navigating = False
                self.main_frame = msg['params']['frameId']
            if self.main_frame == msg['params']['frameId']:
                logging.debug("Navigating main frame")
                self.last_activity = monotonic.monotonic()
                self.page_loaded = None
        elif event == 'frameStoppedLoading' and 'params' in msg and 'frameId' in msg['params']:
            if self.main_frame is not None and \
                    not self.page_loaded and \
                    self.main_frame == msg['params']['frameId']:
                if self.nav_error is not None:
                    self.task['error'] = self.nav_error
                    logging.debug("Page load failed: %s", self.nav_error)
                self.page_loaded = monotonic.monotonic()
        elif event == 'javascriptDialogOpening':
            result = self.send_command("Page.handleJavaScriptDialog", {"accept": False}, wait=True)
            if result is not None and 'error' in result:
                result = self.send_command("Page.handleJavaScriptDialog",
                                           {"accept": True}, wait=True)
                if result is not None and 'error' in result:
                    self.task['error'] = "Page opened a modal dailog"
        elif event == 'interstitialShown':
            self.main_thread_blocked = True
            logging.debug("Page opened a modal interstitial")
            self.nav_error = "Page opened a modal interstitial"

    def process_network_event(self, event, msg):
        """Process Network.* dev tools events"""
        if 'requestId' in msg['params']:
            request_id = msg['params']['requestId']
            if request_id not in self.requests:
                self.requests[request_id] = {'id': request_id}
            request = self.requests[request_id]
            ignore_activity = request['is_video'] if 'is_video' in request else False
            if event == 'requestWillBeSent':
                if 'request' not in request:
                    request['request'] = []
                request['request'].append(msg['params'])
                if 'url' in msg['params'] and msg['params']['url'].endswith('.mp4'):
                    request['is_video'] = True
                request['fromNet'] = True
                if self.main_frame is not None and \
                        self.main_request is None and \
                        'frameId' in msg['params'] and \
                        msg['params']['frameId'] == self.main_frame:
                    logging.debug('Main request detected')
                    self.main_request = request_id
                    if 'timestamp' in msg['params']:
                        self.start_timestamp = float(msg['params']['timestamp'])
            elif event == 'resourceChangedPriority':
                if 'priority' not in request:
                    request['priority'] = []
                request['priority'].append(msg['params'])
            elif event == 'requestServedFromCache':
                request['fromNet'] = False
            elif event == 'responseReceived':
                if 'response' not in request:
                    request['response'] = []
                request['response'].append(msg['params'])
                if 'response' in msg['params']:
                    if 'fromDiskCache' in msg['params']['response'] and \
                            msg['params']['response']['fromDiskCache']:
                        request['fromNet'] = False
                    if 'mimeType' in msg['params']['response'] and \
                            msg['params']['response']['mimeType'].startswith('video/'):
                        request['is_video'] = True
            elif event == 'dataReceived':
                if 'data' not in request:
                    request['data'] = []
                request['data'].append(msg['params'])
            elif event == 'loadingFinished':
                request['finished'] = msg['params']
            elif event == 'loadingFailed':
                request['failed'] = msg['params']
                if self.main_request is not None and \
                        request_id == self.main_request and \
                        'errorText' in msg['params'] and \
                        'canceled' in msg['params'] and \
                        not msg['params']['canceled']:
                    self.nav_error = msg['params']['errorText']
                    logging.debug('Navigation error: %s', self.nav_error)
            else:
                ignore_activity = True
            if not self.task['stop_at_onload'] and not ignore_activity:
                self.last_activity = monotonic.monotonic()

    def process_inspector_event(self, event):
        """Process Inspector.* dev tools events"""
        if event == 'detached':
            self.task['error'] = 'Inspector detached, possibly crashed.'
        elif event == 'targetCrashed':
            self.task['error'] = 'Browser crashed.'

    def log_dev_tools_event(self, msg):
        """Log the dev tools events to a file"""
        if self.task['log_data']:
            if self.dev_tools_file is None:
                path = self.path_base + '_devtools.json.gz'
                self.dev_tools_file = gzip.open(path, 'wb', 7)
                self.dev_tools_file.write("[{}")
            if self.dev_tools_file is not None:
                self.dev_tools_file.write(",\n")
                self.dev_tools_file.write(json.dumps(msg))

    def get_header_value(self, headers, name):
        """Get the value for the requested header"""
        value = None
        if headers:
            if name in headers:
                value = headers[name]
            else:
                find = name.lower()
                for header_name in headers:
                    check = header_name.lower()
                    if check == find or (check[0] == ':' and check[1:] == find):
                        value = headers[header_name]
                        break
        return value

    def bytes_from_range(self, text, range_info):
        """Convert a line/column start and end into a byte count"""
        byte_count = 0
        try:
            lines = text.splitlines()
            line_count = len(lines)
            start_line = range_info['startLine']
            end_line = range_info['endLine']
            if start_line > line_count or end_line > line_count:
                return 0
            start_column = range_info['startColumn']
            end_column = range_info['endColumn']
            if start_line == end_line:
                byte_count = end_column - start_column + 1
            else:
                # count the whole lines between the partial start and end lines
                if end_line > start_line + 1:
                    for row in xrange(start_line + 1, end_line):
                        byte_count += len(lines[row])
                byte_count += len(lines[start_line][start_column:])
                byte_count += end_column
        except Exception:
            pass
        return byte_count

class DevToolsClient(WebSocketClient):
    """DevTools Websocket client"""
    def __init__(self, url, protocols=None, extensions=None, heartbeat_freq=None,
                 ssl_options=None, headers=None):
        WebSocketClient.__init__(self, url, protocols, extensions, heartbeat_freq,
                                 ssl_options, headers)
        self.connected = False
        self.messages = Queue.Queue()
        self.trace_file = None
        self.video_prefix = None
        self.trace_ts_start = None
        self.options = None
        self.job = None
        self.task = None
        self.last_image = None
        self.pending_image = None
        self.video_viewport = None
        self.path_base = None
        self.trace_parser = None
        self.trace_event_counts = {}

    def opened(self):
        """Websocket interface - connection opened"""
        logging.debug("DevTools websocket connected")
        self.connected = True

    def closed(self, code, reason=None):
        """Websocket interface - connection closed"""
        logging.debug("DevTools websocket disconnected")
        self.connected = False

    def received_message(self, raw):
        """Websocket interface - message received"""
        try:
            if raw.is_text:
                message = raw.data.decode(raw.encoding) if raw.encoding is not None else raw.data
                compare = message[:50]
                is_trace_data = False
                if self.path_base is not None and compare.find('"Tracing.dataCollected') > -1:
                    is_trace_data = True
                    msg = json.loads(message)
                    self.messages.put('{"method":"got_message"}')
                    if msg is not None:
                        self.process_trace_event(msg)
                elif self.trace_file is not None and compare.find('"Tracing.tracingComplete') > -1:
                    self.trace_file.write("\n]}")
                    self.trace_file.close()
                    self.trace_file = None
                if not is_trace_data:
                    self.messages.put(message)
        except Exception:
            pass

    def get_message(self, timeout):
        """Wait for and return a message from the queue"""
        message = None
        try:
            if timeout is None or timeout <= 0:
                message = self.messages.get_nowait()
            else:
                message = self.messages.get(True, timeout)
            self.messages.task_done()
        except Exception:
            pass
        return message

    def start_processing_trace(self, path_base, video_prefix, options, job, task, start_timestamp):
        """Write any trace events to the given file"""
        self.last_image = None
        self.trace_ts_start = None
        if start_timestamp is not None:
            self.trace_ts_start = int(start_timestamp * 1000000)
        self.path_base = path_base
        self.video_prefix = video_prefix
        self.task = task
        self.options = options
        self.job = job
        self.video_viewport = None

    def stop_processing_trace(self):
        """All done"""
        if self.pending_image is not None and self.last_image is not None and\
                self.pending_image["image"] != self.last_image["image"]:
            with open(self.pending_image["path"], 'wb') as image_file:
                image_file.write(base64.b64decode(self.pending_image["image"]))
        self.pending_image = None
        self.trace_ts_start = None
        if self.trace_file is not None:
            self.trace_file.write("\n]}")
            self.trace_file.close()
            self.trace_file = None
        self.options = None
        self.job = None
        self.task = None
        self.video_viewport = None
        self.last_image = None
        if self.trace_parser is not None and self.path_base is not None:
            start = monotonic.monotonic()
            logging.debug("Post-Processing the trace netlog events")
            self.trace_parser.post_process_netlog_events()
            logging.debug("Processing the trace timeline events")
            self.trace_parser.ProcessTimelineEvents()
            self.trace_parser.WriteUserTiming(self.path_base + '_user_timing.json.gz')
            self.trace_parser.WriteCPUSlices(self.path_base + '_timeline_cpu.json.gz')
            self.trace_parser.WriteScriptTimings(self.path_base + '_script_timing.json.gz')
            self.trace_parser.WriteFeatureUsage(self.path_base + '_feature_usage.json.gz')
            self.trace_parser.WriteInteractive(self.path_base + '_interactive.json.gz')
            self.trace_parser.WriteNetlog(self.path_base + '_netlog_requests.json.gz')
            self.trace_parser.WriteV8Stats(self.path_base + '_v8stats.json.gz')
            elapsed = monotonic.monotonic() - start
            logging.debug("Done processing the trace events: %0.3fs", elapsed)
        self.trace_parser = None
        self.path_base = None
        logging.debug(self.trace_event_counts)
        self.trace_event_counts = {}

    def process_trace_event(self, msg):
        """Process Tracing.* dev tools events"""
        if 'params' in msg and 'value' in msg['params'] and len(msg['params']['value']):
            if self.trace_file is None:
                self.trace_file = gzip.open(self.path_base + '_trace.json.gz',
                                            'wb', compresslevel=7)
                self.trace_file.write('{"traceEvents":[{}')
                from internal.support.trace_parser import Trace
                self.trace_parser = Trace()
            # write out the trace events one-per-line but pull out any
            # devtools screenshots as separate files.
            if self.trace_file is not None:
                trace_events = msg['params']['value']
                for _, trace_event in enumerate(trace_events):
                    is_screenshot = False
                    if self.video_prefix is not None and 'cat' in trace_event and \
                            'name' in trace_event and 'ts' in trace_event:
                        if trace_event['cat'] not in self.trace_event_counts:
                            self.trace_event_counts[trace_event['cat']] = 0
                        self.trace_event_counts[trace_event['cat']] += 1
                        if self.trace_ts_start is None and \
                                (trace_event['name'] == 'navigationStart' or \
                                 trace_event['name'] == 'fetchStart') and \
                                trace_event['cat'].find('blink.user_timing') > -1:
                            logging.debug("Trace start detected: %d", trace_event['ts'])
                            self.trace_ts_start = trace_event['ts']
                        if self.trace_ts_start is None and \
                                (trace_event['name'] == 'navigationStart' or \
                                 trace_event['name'] == 'fetchStart') and \
                                trace_event['cat'].find('rail') > -1:
                            logging.debug("Trace start detected: %d", trace_event['ts'])
                            self.trace_ts_start = trace_event['ts']
                        if trace_event['name'] == 'Screenshot' and \
                                trace_event['cat'].find('devtools.screenshot') > -1:
                            is_screenshot = True
                            self.process_screenshot(trace_event)
                    if not is_screenshot:
                        # Write it to the trace file and pass it to the trace parser
                        self.trace_file.write(",\n")
                        self.trace_file.write(json.dumps(trace_event))
                        if self.trace_parser is not None:
                            self.trace_parser.ProcessTraceEvent(trace_event)
                logging.debug("Processed %d trace events", len(msg['params']['value']))

    def process_screenshot(self, trace_event):
        """Process an individual screenshot event"""
        if self.trace_ts_start is not None and 'args' in trace_event and \
                'snapshot' in trace_event['args']:
            ms_elapsed = int(round(float(trace_event['ts'] - self.trace_ts_start) / 1000.0))
            if ms_elapsed >= 0:
                img = trace_event['args']['snapshot']
                path = '{0}{1:06d}.jpg'.format(self.video_prefix, ms_elapsed)
                logging.debug("Video frame (%f): %s", trace_event['ts'], path)
                # Sample frames at at 100ms intervals for the first 20 seconds,
                # 500ms for 20-40seconds and 2 second intervals after that
                min_interval = 100
                if ms_elapsed > 40000:
                    min_interval = 2000
                elif ms_elapsed > 20000:
                    min_interval = 500
                keep_image = True
                if self.last_image is not None:
                    elapsed_interval = ms_elapsed - self.last_image["time"]
                    if elapsed_interval < min_interval:
                        keep_image = False
                        if self.pending_image is not None:
                            logging.debug("Discarding pending image: %s",
                                          self.pending_image["path"])
                        self.pending_image = {"image": str(img),
                                              "time": int(ms_elapsed),
                                              "path": str(path)}
                if keep_image:
                    is_duplicate = False
                    if self.pending_image is not None:
                        if self.pending_image["image"] == img:
                            is_duplicate = True
                    elif self.last_image is not None and \
                            self.last_image["image"] == img:
                        is_duplicate = True
                    if is_duplicate:
                        logging.debug('Dropping duplicate image: %s', path)
                    else:
                        # write both the pending image and the current one if
                        # the interval is double the normal sampling rate
                        if self.last_image is not None and self.pending_image is not None and \
                                self.pending_image["image"] != self.last_image["image"]:
                            elapsed_interval = ms_elapsed - self.last_image["time"]
                            if elapsed_interval > 2 * min_interval:
                                pending = self.pending_image["path"]
                                with open(pending, 'wb') as image_file:
                                    image_file.write(base64.b64decode(self.pending_image["image"]))
                        self.pending_image = None
                        with open(path, 'wb') as image_file:
                            self.last_image = {"image": str(img),
                                               "time": int(ms_elapsed),
                                               "path": str(path)}
                            image_file.write(base64.b64decode(img))
