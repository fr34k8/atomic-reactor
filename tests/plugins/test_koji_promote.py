"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import json
import os

try:
    import koji
except ImportError:
    import inspect
    import os
    import sys

    # Find out mocked koji module
    import tests.koji as koji
    mock_koji_path = os.path.dirname(inspect.getfile(koji.ClientSession))
    if mock_koji_path not in sys.path:
        sys.path.append(os.path.dirname(mock_koji_path))

    # Now load it properly, the same way the plugin will
    del koji
    import koji

from atomic_reactor.core import DockerTasker
from atomic_reactor.plugins.exit_koji_promote import KojiPromotePlugin
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from atomic_reactor.plugin import ExitPluginsRunner, PluginFailedException
from atomic_reactor.inner import DockerBuildWorkflow, TagConf
from atomic_reactor.util import ImageName
from atomic_reactor.source import GitSource, PathSource
from tests.constants import SOURCE

from flexmock import flexmock
import pytest
from tests.docker_mock import mock_docker
import subprocess


class X(object):
    pass


class MockedClientSession(object):
    def __init__(self, hub):
        pass

    def importGeneratedContent(self, metadata):
        self.metadata = metadata


FAKE_RPM_OUTPUT = ('name1,1.0,1,x86_64,0,01234567,(none),abcdef01234567\n'
                   'name2,2.0,2,x86_64,0,12345678,(none),bcdef012345678\n\n')

FAKE_OS_OUTPUT = 'fedora-22'


def fake_subprocess_output(cmd):
    if cmd.startswith('/bin/rpm'):
        return FAKE_RPM_OUTPUT
    elif cmd.find('os-release') != -1:
        return FAKE_OS_OUTPUT
    else:
        raise RuntimeError


class MockedPopen(object):
    def __init__(self, cmd, *args, **kwargs):
        self.cmd = cmd

    def wait(self):
        return 0

    def communicate(self):
        return (fake_subprocess_output(self.cmd), '')


def fake_Popen(cmd, *args, **kwargs):
    return MockedPopen(cmd, *args, **kwargs)


def prepare(session=None, name=None, version=None, release=None, source=None,
            build_process_failed=None):
    if session is None:
        session = MockedClientSession('')
    if source is None:
        source = GitSource('git', 'git://hostname/path')
    if build_process_failed is None:
        build_process_failed = False

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'image_id', 'asd123')
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='22'))
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)
    setattr(workflow, 'tagconf', TagConf())
    flexmock(koji, ClientSession=lambda hub: session)
    flexmock(GitSource)
    setattr(workflow, 'source', source)
    setattr(workflow.source, 'lg', X())
    setattr(workflow.source.lg, 'commit_id', '123456')
    setattr(workflow, 'build_results', ['docker build log\n'])
    setattr(workflow, 'exported_image_sequence', [{'path': '/dev/null'}])
    setattr(workflow, 'build_failed', build_process_failed)
    workflow.postbuild_results[PostBuildRPMqaPlugin.key] = "\n".join([
        "name1,1.0,1,x86_64,0,2000,01234567,23000",
        "name2,2.0,1,x86_64,0,3000,abcdef01,24000",
    ])
    mock_docker()
    flexmock(subprocess, Popen=fake_Popen)
    try:
        # py3
        subprocess.getstatusoutput
        flexmock(subprocess,
                 getstatusoutput=lambda cmd: (0, fake_subprocess_output(cmd)))
    except AttributeError:
        # py2
        pass

    if name and version and release:
        workflow.tagconf.add_primary_images(["{0}:{1}_{2}".format(name,
                                                                  version,
                                                                  release),
                                             "{0}:{1}".format(name, version),
                                             "{0}:latest".format(name)])

    runner = ExitPluginsRunner(tasker, workflow,
                                    [
                                        {
                                            'name': KojiPromotePlugin.key,
                                            'args': {
                                                'hub': ''
                                            }
                                        }
                                    ])

    os.environ["BUILD"] = json.dumps({
        "metadata": {
            "creationTimestamp": "2015-07-27T09:24:00Z"
        }
    })

    return runner


def test_koji_promote_failed_build():
    session = MockedClientSession('')
    runner = prepare(build_process_failed=True)
    runner.run()

    # Must not have promoted this build
    assert not hasattr(session, 'metadata')


def test_koji_promote_no_tagconf():
    runner = prepare()
    with pytest.raises(PluginFailedException):
        runner.run()


def test_koji_promote_no_build_env():
    runner = prepare(name='name', version='1.0', release='1')

    # No BUILD environment variable
    if "BUILD" in os.environ:
        del os.environ["BUILD"]
    with pytest.raises(PluginFailedException):
        runner.run()


def test_koji_promote_no_build_metadata():
    runner = prepare(name='name', version='1.0', release='1')

    # No BUILD metadata
    os.environ["BUILD"] = json.dumps({})
    with pytest.raises(PluginFailedException):
        runner.run()


def test_koji_promote_invalid_creation_timestamp():
    runner = prepare(name='name', version='1.0', release='1')

    # Invalid timestamp format
    os.environ["BUILD"] = json.dumps({
        "metadata": {
            "creationTimestamp": "2015-07-27 09:24 UTC"
        }
    })
    with pytest.raises(PluginFailedException):
        runner.run()


def test_koji_promote_wrong_source_type():
    runner = prepare(name='name', version='1.0', release='1',
                     source=PathSource('path', 'file:///dev/null'))
    with pytest.raises(PluginFailedException):
        runner.run()


def test_koji_promote():
    session = MockedClientSession('')
    name = 'name'
    version = '1.0'
    release = '1'
    runner = prepare(session, name=name, version=version, release=release)
    runner.run()

    data = session.metadata
    print("JSON to submit: %s" % json.dumps(data, sort_keys=True, indent=4))
    assert data['metadata_version'] in ['0', 0]

    build = data['build']
    assert isinstance(build, dict)

    buildroots = data['buildroots']
    assert isinstance(buildroots, list)

    output_files = data['output']
    assert isinstance(output_files, list)

    assert 'name' in build
    assert build['name'] == name
    assert 'version' in build
    assert build['version'] == version
    assert 'release' in build
    assert build['release'] == release
    assert 'source' in build
    assert build['source'] == 'git://hostname/path#123456'
    assert 'start_time' in build
    assert int(build['start_time']) > 0
    assert 'end_time' in build
    assert int(build['end_time']) > 0

    for buildroot in buildroots:
        assert isinstance(buildroot, dict)

        assert 'host' in buildroot
        host = buildroot['host']
        assert 'os' in host
        assert 'arch' in host

        assert 'content_generator' in buildroot
        content_generator = buildroot['content_generator']
        assert 'name' in content_generator
        assert 'version' in content_generator
        #assert content_generator['version']

        assert 'container' in buildroot
        container = buildroot['container']
        assert 'type' in container
        assert 'arch' in container

        assert 'tools' in buildroot
        assert isinstance(buildroot['tools'], list)
        assert len(buildroot['tools']) > 0
        for tool in buildroot['tools']:
            assert isinstance(tool, dict)
            assert 'name' in tool
            assert 'version' in tool
            assert tool['version']

        assert 'component_rpms' in buildroot
        assert isinstance(buildroot['component_rpms'], list)
        assert len(buildroot['component_rpms']) > 0
        for component_rpm in buildroot['component_rpms']:
            assert isinstance(component_rpm, dict)
            assert 'name' in component_rpm
            assert 'version' in component_rpm
            assert 'release' in component_rpm
            assert 'epoch' in component_rpm
            assert 'arch' in component_rpm
            assert 'sigmd5' in component_rpm
            assert 'signature' in component_rpm
            assert component_rpm['signature'] != '(none)'

        assert 'component_archives' in buildroot
        assert isinstance(buildroot['component_archives'], list)
        for component_archive in buildroot['component_archives']:
            assert isinstance(component_archive, dict)
            assert 'filename' in component_archive
            assert 'filesize' in component_archive
            assert 'checksum' in component_archive
            assert 'checksum_type' in component_archive

        assert 'extra' in buildroot
        extra = buildroot['extra']
        assert isinstance(extra, dict)
        #assert 'osbs' in extra
        #osbs = extra['osbs']
        #assert isinstance(osbs, dict)
        #assert 'build_id' in osbs
        #assert 'builder_image_id' in osbs

    for output in output_files:
        assert isinstance(output, dict)
        assert 'filename' in output
        assert 'filesize' in output
        assert 'arch' in output
        assert 'checksum' in output
        assert 'checksum_type' in output
        assert 'type' in output
        if output['type'] == 'log':
            assert output['arch'] == 'noarch'
        elif output['type'] == 'image':
            assert output['arch'] != 'noarch'
            #assert 'component_rpms' in output
            #assert 'component_archives' in output
