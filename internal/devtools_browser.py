# Copyright 2017 Google Inc. All rights reserved.
# Use of this source code is governed by the Apache 2.0 license that can be
# found in the LICENSE file.
"""Base class support for browsers that speak the dev tools protocol"""
import glob
import gzip
import logging
import os
import re
import shutil
import subprocess
import time
import monotonic
import ujson as json
from .optimization_checks import OptimizationChecks

class DevtoolsBrowser(object):
    """Devtools Browser base"""
    CONNECT_TIME_LIMIT = 30
    CURRENT_VERSION = 1

    def __init__(self, options, job, use_devtools_video=True):
        self.options = options
        self.job = job
        self.devtools = None
        self.task = None
        self.event_name = None
        self.browser_version = None
        self.use_devtools_video = use_devtools_video
        self.support_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'support')
        self.script_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'js')

    def connect(self, task):
        """Connect to the dev tools interface"""
        ret = False
        from internal.devtools import DevTools
        self.devtools = DevTools(self.options, self.job, task, self.use_devtools_video)
        if task['running_lighthouse']:
            self.devtools.wait_for_available(self.CONNECT_TIME_LIMIT)
        else:
            if self.devtools.connect(self.CONNECT_TIME_LIMIT):
                logging.debug("Devtools connected")
                ret = True
            else:
                task['error'] = "Error connecting to dev tools interface"
                logging.critical(task['error'])
                self.devtools = None
        return ret

    def disconnect(self):
        """Disconnect from dev tools"""
        if self.devtools is not None:
            self.devtools.close()
            self.devtools = None

    def prepare_browser(self, task):
        """Prepare the running browser (mobile emulation, UA string, etc"""
        if self.devtools is not None:
            # Clear the caches
            if not task['cached']:
                self.devtools.send_command("Network.clearBrowserCache", {},
                                           wait=True)
                self.devtools.send_command("Network.clearBrowserCookies", {},
                                           wait=True)

            # Mobile Emulation
            if not self.options.android and \
                    'mobile' in self.job and self.job['mobile'] and \
                    'width' in self.job and 'height' in self.job and \
                    'dpr' in self.job:
                self.devtools.send_command("Emulation.setDeviceMetricsOverride",
                                           {"width":
                                            int(re.search(r'\d+',
                                                          str(self.job['width'])).group()),
                                            "height":
                                            int(re.search(r'\d+',
                                                          str(self.job['height'])).group()),
                                            "screenWidth":
                                            int(re.search(r'\d+',
                                                          str(self.job['width'])).group()),
                                            "screenHeight":
                                            int(re.search(r'\d+',
                                                          str(self.job['height'])).group()),
                                            "scale": 1,
                                            "positionX": 0,
                                            "positionY": 0,
                                            "deviceScaleFactor": float(self.job['dpr']),
                                            "mobile": True,
                                            "fitWindow": False},
                                           wait=True)
                self.devtools.send_command("Emulation.setVisibleSize",
                                           {"width":
                                            int(re.search(r'\d+',
                                                          str(self.job['width'])).group()),
                                            "height":
                                            int(re.search(r'\d+',
                                                          str(self.job['height'])).group())},
                                           wait=True)
            # UA String
            ua_string = self.devtools.execute_js("navigator.userAgent")
            if ua_string is not None:
                match = re.search(r'Chrome\/(\d+\.\d+\.\d+\.\d+)', ua_string)
                if match:
                    self.browser_version = match.group(1)
            if 'uastring' in self.job:
                ua_string = self.job['uastring']
            if ua_string is not None and 'keepua' not in self.job or not self.job['keepua']:
                ua_string += ' PTST/{0:d}'.format(self.CURRENT_VERSION)
            if ua_string is not None:
                self.job['user_agent_string'] = ua_string
            # Disable js
            if self.job['noscript']:
                self.devtools.send_command("Emulation.setScriptExecutionDisabled",
                                           {"value": True}, wait=True)

    def on_start_recording(self, task):
        """Start recording"""
        if self.browser_version is not None and 'browserVersion' not in task['page_data']:
            task['page_data']['browserVersion'] = self.browser_version
        if self.devtools is not None:
            self.devtools.start_recording()

    def on_stop_recording(self, task):
        """Stop recording"""
        if self.devtools is not None:
            if self.job['pngss']:
                screen_shot = os.path.join(task['dir'],
                                           task['prefix'] + '_screen.png')
                self.devtools.grab_screenshot(screen_shot, png=True)
            else:
                screen_shot = os.path.join(task['dir'],
                                           task['prefix'] + '_screen.jpg')
                self.devtools.grab_screenshot(screen_shot, png=False, resize=600)
            # Collect end of test data from the browser
            self.collect_browser_metrics(task)
            # Stop recording dev tools (which also collects the trace)
            self.devtools.stop_recording()

    def run_task(self, task):
        """Run an individual test"""
        if self.devtools is not None:
            self.task = task
            logging.debug("Running test")
            end_time = monotonic.monotonic() + task['time_limit']
            task['current_step'] = 1
            recording = False
            while len(task['script']) and task['error'] is None and \
                    monotonic.monotonic() < end_time:
                self.prepare_task(task)
                command = task['script'].pop(0)
                if not recording and command['record']:
                    recording = True
                    self.on_start_recording(task)
                self.process_command(command)
                if command['record']:
                    self.devtools.wait_for_page_load()
                    if not task['combine_steps'] or not len(task['script']):
                        self.on_stop_recording(task)
                        recording = False
                        self.on_start_processing(task)
                        self.wait_for_processing(task)
                        self.process_devtools_requests(task)
                        if task['log_data']:
                            # Move on to the next step
                            task['current_step'] += 1
                            self.event_name = None
            # Always navigate to about:blank after finishing in case the tab is
            # remembered across sessions
            if task['error'] is None:
                self.devtools.send_command('Page.navigate', {'url': 'about:blank'}, wait=True)
            self.task = None

    def on_start_processing(self, task):
        """Start any processing of the captured data"""
        if task['log_data']:
            # Start the processing that can run in a background thread
            optimization = OptimizationChecks(self.job, task, self.get_requests())
            optimization.start()
            # Run the video post-processing
            if self.use_devtools_video and  self.job['video']:
                self.process_video()
            optimization.join()

    def wait_for_processing(self, task):
        """Stub for override"""
        pass

    def prepare_task(self, task):
        """Format the file prefixes for multi-step testing"""
        task['page_data'] = {}
        if task['current_step'] == 1:
            task['prefix'] = task['task_prefix']
            task['video_subdirectory'] = task['task_video_prefix']
        else:
            task['prefix'] = '{0}_{1:d}'.format(task['task_prefix'], task['current_step'])
            task['video_subdirectory'] = '{0}_{1:d}'.format(task['task_video_prefix'],
                                                            task['current_step'])
        if task['video_subdirectory'] not in task['video_directories']:
            task['video_directories'].append(task['video_subdirectory'])
        if self.event_name is not None:
            task['step_name'] = self.event_name
        else:
            task['step_name'] = 'Step_{0:d}'.format(task['current_step'])

    def process_video(self):
        """Post process the video"""
        from internal.video_processing import VideoProcessing
        video = VideoProcessing(self.job, self.task)
        video.process()

    def process_devtools_requests(self, task):
        """Process the devtools log and pull out the requests information"""
        path_base = os.path.join(self.task['dir'], self.task['prefix'])
        devtools_file = path_base + '_devtools.json.gz'
        if os.path.isfile(devtools_file):
            devtools_parser = os.path.join(self.support_path, "devtools_parser.py")
            cmd = ['python', devtools_parser, '-vvvv', '--devtools', devtools_file]
            netlog = path_base + '_netlog_requests.json.gz'
            if os.path.isfile(netlog):
                cmd.extend(['--netlog', netlog])
            optimization = path_base + '_optimization.json.gz'
            if os.path.isfile(optimization):
                cmd.extend(['--optimization', optimization])
            if task['cached']:
                cmd.append('--cached')
            out_file = path_base + '_devtools_requests.json.gz'
            cmd.extend(['--out', out_file])
            logging.debug(cmd)
            subprocess.call(cmd)

    def run_js_file(self, file_name):
        """Execute one of our js scripts"""
        ret = None
        script = None
        script_file_path = os.path.join(self.script_dir, file_name)
        if os.path.isfile(script_file_path):
            with open(script_file_path, 'rb') as script_file:
                script = script_file.read()
        if script is not None:
            ret = self.devtools.execute_js(script)
        return ret

    def collect_browser_metrics(self, task):
        """Collect all of the in-page browser metrics that we need"""
        user_timing = self.run_js_file('user_timing.js')
        if user_timing is not None:
            path = os.path.join(task['dir'], task['prefix'] + '_timed_events.json.gz')
            with gzip.open(path, 'wb', 7) as outfile:
                outfile.write(json.dumps(user_timing))
        page_data = self.run_js_file('page_data.js')
        if page_data is not None:
            task['page_data'].update(page_data)
        if 'customMetrics' in self.job:
            custom_metrics = {}
            for name in self.job['customMetrics']:
                script = 'var wptCustomMetric = function() {' +\
                         self.job['customMetrics'][name] +\
                         '};try{wptCustomMetric();}catch(e){};'
                custom_metrics[name] = self.devtools.execute_js(script)
            path = os.path.join(task['dir'], task['prefix'] + '_metrics.json.gz')
            with gzip.open(path, 'wb', 7) as outfile:
                outfile.write(json.dumps(custom_metrics))

    def process_command(self, command):
        """Process an individual script command"""
        logging.debug("Processing script command:")
        logging.debug(command)
        if command['command'] == 'navigate':
            self.devtools.start_navigating()
            self.devtools.send_command('Page.navigate', {'url': command['target']})
        elif command['command'] == 'logdata':
            self.task['combine_steps'] = False
            if int(re.search(r'\d+', str(command['target'])).group()):
                logging.debug("Data logging enabled")
                self.task['log_data'] = True
            else:
                logging.debug("Data logging disabled")
                self.task['log_data'] = False
        elif command['command'] == 'combinesteps':
            self.task['log_data'] = True
            self.task['combine_steps'] = True
        elif command['command'] == 'seteventname':
            self.event_name = command['target']
        elif command['command'] == 'exec':
            if command['record']:
                self.devtools.start_navigating()
            self.devtools.execute_js(command['target'])
        elif command['command'] == 'sleep':
            delay = min(60, max(0, int(re.search(r'\d+', str(command['target'])).group())))
            if delay > 0:
                time.sleep(delay)
        elif command['command'] == 'setabm':
            self.task['stop_at_onload'] = bool('target' in command and \
                                               int(re.search(r'\d+',
                                                             str(command['target'])).group()) == 0)
        elif command['command'] == 'setactivitytimeout':
            if 'target' in command:
                self.task['activity_time'] = \
                    max(0, min(30, int(re.search(r'\d+', str(command['target'])).group())))
        elif command['command'] == 'setuseragent':
            self.task['user_agent_string'] = command['target']
        elif command['command'] == 'setcookie':
            if 'target' in command and 'value' in command:
                url = command['target'].strip()
                cookie = command['value']
                pos = cookie.find(';')
                if pos > 0:
                    cookie = cookie[:pos]
                pos = cookie.find('=')
                if pos > 0:
                    name = cookie[:pos].strip()
                    value = cookie[pos+1:].strip()
                    if len(name) and len(value) and len(url):
                        self.devtools.send_command('Network.setCookie',
                                                   {'url': url, 'name': name, 'value': value})

    def navigate(self, url):
        """Navigate to the given URL"""
        if self.devtools is not None:
            self.devtools.send_command('Page.navigate', {'url': url}, wait=True)

    def get_requests(self):
        """Get the request details for running an optimization check"""
        requests = None
        if self.devtools is not None:
            requests = self.devtools.get_requests()
        return requests

    def run_lighthouse_test(self, task):
        """Run a lighthouse test against the current browser session"""
        from threading import Timer
        if 'url' in self.job and self.job['url'] is not None:
            output_path = os.path.join(task['dir'], 'lighthouse.json')
            json_file = os.path.join(task['dir'], 'lighthouse.report.json')
            json_gzip = os.path.join(task['dir'], 'lighthouse.json.gz')
            html_file = os.path.join(task['dir'], 'lighthouse.report.html')
            html_gzip = os.path.join(task['dir'], 'lighthouse.html.gz')
            command = ['lighthouse',
                       '--disable-network-throttling',
                       '--port', str(task['port']),
                       '--output', 'html',
                       '--output', 'json',
                       '--output-path', '"{0}"'.format(output_path)]
            if self.job['keep_lighthouse_trace']:
                command.append('--save-assets')
            if self.options.android or 'mobile' not in self.job or not self.job['mobile']:
                command.extend(['--disable-device-emulation', '--disable-cpu-throttling'])
            command.append('"{0}"'.format(self.job['url']))
            cmd = ' '.join(command)
            logging.debug(cmd)
            # Give lighthouse up to 10 minutes to run (safety for hung test)
            proc = subprocess.Popen(cmd, shell=True)
            timer = None
            try:
                timer = Timer(600, proc.kill)
                timer.start()
                proc.communicate()
            except Exception:
                logging.debug('Timeout running lighthouse test')
            finally:
                if timer is not None:
                    timer.cancel()
            # Rename and compress the trace file, delete the other assets
            if self.job['keep_lighthouse_trace']:
                try:
                    lh_trace_src = os.path.join(task['dir'], 'lighthouse-0.trace.json')
                    if os.path.isfile(lh_trace_src):
                        # read the JSON in and re-write it line by line to match the other traces
                        with open(lh_trace_src, 'rb') as f_in:
                            trace = json.load(f_in)
                            if trace is not None and 'traceEvents' in trace:
                                lighthouse_trace = os.path.join(task['dir'],
                                                                'lighthouse_trace.json.gz')
                            with gzip.open(lighthouse_trace, 'wb', 7) as f_out:
                                f_out.write('{"traceEvents":[{}')
                                for trace_event in trace['traceEvents']:
                                    f_out.write(",\n")
                                    f_out.write(json.dumps(trace_event))
                                f_out.write("\n]}")
                except Exception:
                    pass
            # Delete all the left-over lighthouse assets
            files = glob.glob(os.path.join(task['dir'], 'lighthouse-*'))
            for file_path in files:
                try:
                    os.remove(file_path)
                except Exception:
                    pass
            if os.path.isfile(json_file):
                # Remove the raw screenshots if they were stored with the file
                lh_report = None
                with open(json_file, 'rb') as f_in:
                    lh_report = json.load(f_in)
                if lh_report is not None and 'audits' in lh_report and \
                        'screenshots' in lh_report['audits']:
                    del lh_report['audits']['screenshots']
                    with gzip.open(json_gzip, 'wb', 7) as f_out:
                        json.dump(lh_report, f_out)
                else:
                    with open(json_file, 'rb') as f_in:
                        with gzip.open(json_gzip, 'wb', 7) as f_out:
                            shutil.copyfileobj(f_in, f_out)
                try:
                    os.remove(json_file)
                except Exception:
                    pass
            if os.path.isfile(html_file):
                # Remove the raw screenshots if they were stored with the file
                with open(html_file, 'rb') as f_in:
                    lh_report = f_in.read()
                    start = lh_report.find('\n    &quot;screenshots')
                    if start >= 0:
                        end = lh_report.find('\n    },', start)
                        if end >= 0:
                            lh_report = lh_report[:start] + lh_report[end + 7:]
                    with gzip.open(html_gzip, 'wb', 7) as f_out:
                        f_out.write(lh_report)
                try:
                    os.remove(html_file)
                except Exception:
                    pass
