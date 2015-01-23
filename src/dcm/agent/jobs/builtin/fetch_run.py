    #  ========= CONFIDENTIAL =========
#
#  Copyright (C) 2010-2014 Dell, Inc. - ALL RIGHTS RESERVED
#
#  ======================================================================
#   NOTICE: All information contained herein is, and remains the property
#   of Dell, Inc. The intellectual and technical concepts contained herein
#   are proprietary to Dell, Inc. and may be covered by U.S. and Foreign
#   Patents, patents in process, and are protected by trade secret or
#   copyright law. Dissemination of this information or reproduction of
#   this material is strictly forbidden unless prior written permission
#   is obtained from Dell, Inc.
#  ======================================================================
import urllib2
import urlparse
from dcm.agent import exceptions
import hashlib
import logging
import os

import dcm.agent.jobs as jobs
import dcm.agent.utils as utils


_g_logger = logging.getLogger(__name__)


class FetchRunScript(jobs.Plugin):

    protocol_arguments = {
        "url": ("The location of the script as a url", True, str, None),
        "connect_timeout": ("The number of seconds to wait to establish a "
                            "connection to the soure url", False, int, 30),
        "checksum": ("The sha256 checksum of the script.", False, str, None),
        "arguments": ("The list of arguments to be passed to the "
                      "downloaded script",
                      False, list, None),
        "ssl_cert": ("The expected cert for the source of the executable "
                     "script to be downloaded.", False,
                      str, None),
    }

    def __init__(self, conf, job_id, items_map, name, arguments):
        super(FetchRunScript, self).__init__(
            conf, job_id, items_map, name, arguments)

    def _do_http_download(self, exe_file, timeout):
        u_req = urllib2.Request(self.args.url)
        u_req.add_header("Content-Type", "application/x-www-form-urlencoded")
        u_req.add_header("Connection", "Keep-Alive")
        u_req.add_header("Cache-Control", "no-cache")

        response = urllib2.urlopen(u_req, timeout=timeout)
        if response.code != 200:
            raise exceptions.AgentRuntimeException("The url %s was invalid"
                                                   % self.args.url)

        sha256 = hashlib.sha256()
        data = response.read(1024)
        with open(exe_file, "wb") as fptr:
            while data:
                sha256.update(data)
                fptr.write(data)
                data = response.read(1024)
        actual_checksum = sha256.hexdigest()
        return actual_checksum

    def run(self):
        exe_file = self.conf.get_temp_file("fetch_exe_file")

        url_parts = urlparse.urlparse(self.args.url)

        if url_parts.scheme != "http" and url_parts.scheme != "https":
            # for now we are only accepting http.  in the future we will
            # switch on scheme to decide what cloud storage protocol module
            # to use
            raise exceptions.AgentOptionValueException(
                "url", url_parts.scheme, "[http, https]")

        try:
            actual_checksum = \
                self._do_http_download(exe_file, self.args.connect_timeout)
        except BaseException as ex:
            reply = {"return_code": 1, "message": "",
                     "error_message": "Failed to download the URL %s: %s" %
                                      (self.args.url, ex.message),
                     "reply_type": "void"}
            return reply

        if self.args.checksum and actual_checksum != self.args.checksum:
            raise exceptions.AgentPluginOperationException(
                "The checksum did not match")
        try:
            os.chmod(exe_file, 0x755)

            command_list = [exe_file]
            if self.args.arguments:
                command_list.extend(self.args.arguments)
            _g_logger.debug("FetchRunScript is running the command %s"
                            % str(command_list))
            (stdout, stderr, rc) = utils.run_command(self.conf, command_list)
            _g_logger.debug("Command %s: stdout %s.  stderr: %s" %
                            (str(command_list), stdout, stderr))
            reply = {"return_code": rc, "message": stdout,
                     "error_message": stderr, "reply_type": "void"}
            return reply
        finally:
            if os.path.exists(exe_file):
                os.remove(exe_file)


def load_plugin(conf, job_id, items_map, name, arguments):
    return FetchRunScript(conf, job_id, items_map, name, arguments)
