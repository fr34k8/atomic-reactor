"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from collections import namedtuple
import json
import hashlib
import os
import subprocess
from tempfile import NamedTemporaryFile
import time

import koji
from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.source import GitSource
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin


# An output file and its metadata
Output = namedtuple('output', ['file', 'metadata'])


def get_os():
    cmd = '. /etc/os-release; echo "$ID-$VERSION_ID"'
    try:
        # py3
        status, output = subprocess.getstatusoutput(cmd)
        if status != 0:
            raise RuntimeError("exit code %s" % status)

        return output
    except AttributeError:
        # py2
        with open('/dev/null', 'r+') as devnull:
            p = subprocess.Popen(cmd,
                                 shell=True,
                                 stdin=devnull,
                                 stdout=subprocess.PIPE,
                                 stderr=devnull)

            (stdout, stderr) = p.communicate()
            status = p.wait()
            if status != 0:
                raise RuntimeError("exit code %s" % status)

            return stdout.decode().rstrip()


def parse_rpm_output(output, tags):
    def field(tag):
        try:
            value = fields[tags.index(tag)]
        except ValueError:
            return None

        if value == '(none)':
            return None

        return value

    component_rpms = []
    for rpm in output.split('\n'):
        fields = rpm.split(',')
        if len(fields) < len(tags):
            continue

        component_rpm = {
            'name': field('NAME'),
            'version': field('VERSION'),
            'release': field('RELEASE'),
            'arch': field('ARCH'),
            'epoch': field('EPOCH'),
            'sigmd5': field('SIGMD5'),
            'signature': (field('SIGPGP') or
                          field('SIGGPG') or
                          None),
        }
        component_rpms.append(component_rpm)

    return component_rpms


def get_rpms():
    tags = [
        'NAME',
        'VERSION',
        'RELEASE',
        'ARCH',
        'EPOCH',
        'SIGMD5',
        'SIGPGP',
        'SIGGPG',
    ]

    fmt = ",".join(["%%{%s}" % tag for tag in tags])
    cmd = "/bin/rpm -qa --qf '{0}\n'".format(fmt)
    try:
        # py3
        (status, output) = subprocess.getstatusoutput(cmd)
    except:
        # py2
        with open('/dev/null', 'r+') as devnull:
            p = subprocess.Popen(cmd,
                                 shell=True,
                                 stdin=devnull,
                                 stdout=subprocess.PIPE,
                                 stderr=devnull)

            (stdout, stderr) = p.communicate()
            status = p.wait()
            output = stdout.decode()

    if status != 0:
        raise RuntimeError("exit code %s" % status)

    return parse_rpm_output(output, tags)


def get_metadata(path, filename):
    metadata = {'filename': filename,
                'filesize': os.path.getsize(path)}
    s = hashlib.sha256()
    blocksize = 65536
    with open(path, mode='rb') as f:
        buf = f.read(blocksize)
        while len(buf) > 0:
            s.update(buf)
            buf = f.read(blocksize)

    metadata.update({'checksum': s.hexdigest(),
                     'checksum_type': 'sha256'})
    return metadata


class KojiPromotePlugin(ExitPlugin):
    """
    Promote this build to Koji

    Runs as an exit plugin in order to capture logs from all other
    plugins.
    """

    key = "koji_promote"
    can_fail = False

    def __init__(self, tasker, workflow, hub):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param hub: string, koji hub (xmlrpc)
        """
        super(KojiPromotePlugin, self).__init__(tasker, workflow)
        self.xmlrpc = koji.ClientSession(hub)
        self.version = tasker.get_version()

    def get_buildroot(self):
        buildroot = {
            'id': 1,
            'host': {
                'os': get_os(),
                'arch': os.uname()[4],
            },
            'content_generator': {
                'name': 'osbs',
                'version': 0,
            },
            'container': {
                'type': 'docker',
                'arch': os.uname()[4],
            },
            'tools': [
                {
                    'name': 'docker',
                    'version': self.version['Version'],
                }
            ],
            'component_rpms': get_rpms(),
            'component_archives': [],
            'extra': {},
        }

        return buildroot

    def get_log(self, prefix, results):
        suffix = ".log"
        # Deleted once closed
        logfile = NamedTemporaryFile(prefix='%s-' % prefix,
                                     suffix='.log',
                                     mode='w')
        json.dump(results, logfile)
        logfile.flush()
        return Output(file=logfile,
                      metadata=get_metadata(logfile.name,
                                            "%s%s" % (prefix, suffix)))

    def get_logs(self):
        return [self.get_log('prebuild', self.workflow.prebuild_results),
                self.get_log('docker', self.workflow.build_results),
                self.get_log('postbuild', self.workflow.postbuild_results)]

    def get_image_component_rpms(self):
        try:
            output = self.workflow.postbuild_results[PostBuildRPMqaPlugin.key]
        except KeyError:
            self.log.error("%s plugin did not run!" %
                           PostBuildRPMqaPlugin.key)
            return []

        return parse_rpm_output(output, PostBuildRPMqaPlugin.rpm_tags)

    def get_output(self, buildroot_id):
        def add_buildroot_id(output):
            logfile, metadata = output
            metadata.update({'buildroot_id': buildroot_id})
            return Output(file=logfile, metadata=metadata)

        def add_log_type(output):
            logfile, metadata = output
            metadata.update({'type': 'log', 'arch': 'noarch'})
            return Output(file=logfile, metadata=metadata)

        output_files = [add_log_type(add_buildroot_id(metadata))
                        for metadata in self.get_logs()]
        
        image_path = self.workflow.exported_image_sequence[-1].get('path')
        metadata = get_metadata(image_path, os.path.basename(image_path))
        metadata.update({
            'arch': os.uname()[4],
            'type': 'image',
            'component_rpms': self.get_image_component_rpms(),
        })
        image = add_buildroot_id(Output(file=None, metadata=metadata))
        output_files.append(image)

        return output_files

    def run(self):
        # Only run if the build was successful
        if self.workflow.build_process_failed:
            return

        try:
            build_json = json.loads(os.environ["BUILD"])
        except KeyError:
            self.log.error("No $BUILD env variable. Probably not running in build container.")
            raise

        try:
            metadata = build_json["metadata"]
            build_start_time = metadata["creationTimestamp"]
        except KeyError:
            self.log.error("No build metadata")
            raise

        try:
            # Decode UTC RFC3339 date with no fractional seconds
            # (the format we expect)
            start_time_struct = time.strptime(build_start_time,
                                              '%Y-%m-%dT%H:%M:%SZ')
            start_time = str(int(time.mktime(start_time_struct)))
        except ValueError:
            self.log.error("Invalid time format (%s)", build_start_time)
            raise

        name = None
        version = None
        release = None
        for image_name in self.workflow.tagconf.primary_images:
            if '_' in image_name.tag:
                name = image_name.repo
                version, release = image_name.tag.split('_', 1)

        if name is None or version is None or release is None:
            raise RuntimeError('Unable to determine name-version-release')

        source = self.workflow.source
        if not isinstance(source, GitSource):
            raise RuntimeError('git source required')

        buildroot = self.get_buildroot()
        output_files = self.get_output(buildroot['id'])

        koji_metadata = {
            'metadata_version': 0,
            'build': {
                'name': name,
                'version': version,
                'release': release,
                'source': "{0}#{1}".format(source.uri, source.commit_id),
                'start_time': start_time,
                'end_time': str(int(time.time()))
            },
            'buildroots': [buildroot],
            'output': [output.metadata for output in output_files],
        }
        self.xmlrpc.importGeneratedContent(koji_metadata)

