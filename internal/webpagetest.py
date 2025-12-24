# Copyright 2019 WebPageTest LLC.
# Copyright 2017 Google Inc.
# Copyright 2020 Catchpoint Systems Inc.
# Use of this source code is governed by the Polyform Shield 1.0.0 license that can be
# found in the LICENSE.md file.
"""Main entry point for interfacing with WebPageTest server"""
import base64
from datetime import datetime
import glob
import gzip
import hashlib
import logging
import multiprocessing
import os
import platform
import random
import re
import shutil
import socket
import string
import subprocess
import sys
import threading
import time
import zipfile
import psutil
from internal import os_util

if (sys.version_info >= (3, 0)):
    from time import monotonic
    from urllib.parse import quote_plus
    from urllib.parse import urlsplit
    GZIP_READ_TEXT = 'rt'
    GZIP_TEXT = 'wt'
else:
    from monotonic import monotonic
    from urllib import quote_plus
    from urlparse import urlsplit
    GZIP_READ_TEXT = 'r'
    GZIP_TEXT = 'w'
try:
    import ujson as json
except BaseException:
    import json

DEFAULT_JPEG_QUALITY = 30


class WebPageTest(object):
    """Controller for interfacing with the WebPageTest server"""
    # pylint: disable=E0611
    def __init__(self, options, workdir):
        import requests
        self.fetch_queue = multiprocessing.JoinableQueue()
        self.fetch_result_queue = multiprocessing.JoinableQueue()
        self.job = None
        self.raw_job = None
        self.first_failure = None
        self.is_rebooting = False
        self.is_dead = False
        self.health_check_server = None
        self.metadata_blocked = False
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'wptagent'})
        self.extension_session = requests.Session()
        self.extension_session.headers.update({'User-Agent': 'wptagent'})
        self.options = options
        self.last_test_id = None
        self.fps = options.fps
        self.test_run_count = 0
        self.log_formatter = logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d - %(message)s",
            datefmt="%H:%M:%S"
        )
        self.log_handler = None

        # Configurable options
        self.work_servers = []
        self.needs_zip = []
        self.url = ''
        if options.server is not None:
            self.work_servers_str = options.server
            if self.work_servers_str == 'www.webpagetest.org':
                self.work_servers_str = 'http://www.webpagetest.org/'
            self.work_servers = self.work_servers_str.split(',')
            self.url = str(self.work_servers[0])

        self.location = ''
        self.test_locations = []
        if options.location is not None:
            self.test_locations = options.location.split(',')
            self.location = str(self.test_locations[0])

        self.wpthost = None
        self.license_pinged = False
        self.key = options.key
        self.scheduler = options.scheduler
        self.scheduler_salt = options.schedulersalt
        self.scheduler_nodes = []
        if options.schedulernode is not None:
            self.scheduler_nodes = options.schedulernode.split(',')
        self.scheduler_node = None
        self.last_diagnostics = None
        self.time_limit = 120
        self.cpu_scale_multiplier = None
        self.pc_name = os_util.pc_name() if options.name is None else options.name
        self.auth_name = options.username
        self.auth_password = options.password if options.password is not None else ''
        self.validate_server_certificate = options.validcertificate
        self.instance_id = None
        self.zone = None
        self.cpu_pct = None

        # Load any locally-defined custom metrics
        self.custom_metrics = {}
        self.load_local_custom_metrics()

        # Warn if no server is configured
        if len(self.work_servers) == 0 and len(self.scheduler_nodes) == 0 and not self.options.pubsub:
            logging.warning(
                "No WebPageTest server configured. Please specify --server option "
                "(e.g., --server http://your-server.com/work/) or --scheduler option."
            )
    # pylint: enable=E0611

    def load_local_custom_metrics(self):
        metrics_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'custom', 'metrics'
        )
        if os.path.isdir(metrics_dir):
            files = glob.glob(metrics_dir + '/*.js')
            for file in files:
                try:
                    with open(file, 'rt') as f:
                        metric_value = f.read()
                        if metric_value:
                            metric_name = os.path.basename(file)[:-3]
                            self.custom_metrics[metric_name] = metric_value
                            logging.debug(
                                'Loaded custom metric %s from %s',
                                metric_name, file
                            )
                except Exception:
                    pass

    def get_test(self, browsers):
        """Get a job from the server"""
        if self.is_rebooting or self.is_dead or self.options.pubsub:
            return
        import requests
        proxies = {"http": None, "https": None}
        from .os_util import get_free_disk_space

        if len(self.work_servers) == 0 and len(self.scheduler_nodes) == 0:
            logging.critical(
                "No work servers or scheduler nodes configured. "
                "Please specify --server or --scheduler options."
            )
            return None

        job = None
        self.raw_job = None
        scheduler_nodes = list(self.scheduler_nodes)
        servers = list(self.work_servers)

        count = 0
        retry = True
        while count < 3 and retry:
            retry = False
            count += 1
            url = self.url + "getwork.php?f=json&shards=1&reboot=1&servers=1&testinfo=1"

            try:
                logging.info("Checking for work: %s", url)
                response = self.session.get(url, timeout=10, proxies=proxies)
                response_text = response.text if len(response.text) else None

                if response_text:
                    job = json.loads(response_text)

            except requests.exceptions.RequestException as err:
                error_msg = str(err)
                if hasattr(err, 'response') and err.response is not None:
                    error_msg = "{} (Status: {})".format(
                        error_msg,
                        err.response.status_code
                    )
                logging.critical(
                    "Get Work Error connecting to %s: %s",
                    url,
                    error_msg
                )
                now = monotonic()
                if self.first_failure is None:
                    self.first_failure = now
                elapsed = now - self.first_failure
                if elapsed > 1800:
                    self.reboot()
                time.sleep(0.1)

            except Exception as e:
                logging.exception(
                    "Unexpected error in get_test: %s",
                    str(e)
                )

        return job
