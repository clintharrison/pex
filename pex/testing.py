# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, print_function

import contextlib
import os
import random
import subprocess
import sys
import tempfile
from collections import namedtuple
from textwrap import dedent

from pex.common import open_zip, safe_mkdir, safe_rmtree, touch
from pex.compatibility import PY3, nested
from pex.executor import Executor
from pex.installer import EggInstaller, Packager
from pex.pex import PEX
from pex.pex_builder import PEXBuilder
from pex.util import DistributionHelper, named_temporary_file

IS_PYPY = "hasattr(sys, 'pypy_version_info')"
NOT_CPYTHON27 = ("%s or (sys.version_info[0], sys.version_info[1]) != (2, 7)" % (IS_PYPY))
NOT_CPYTHON36 = ("%s or (sys.version_info[0], sys.version_info[1]) != (3, 6)" % (IS_PYPY))
IS_LINUX = "platform.system() == 'Linux'"
IS_NOT_LINUX = "platform.system() != 'Linux'"
NOT_CPYTHON27_OR_OSX = "%s or %s" % (NOT_CPYTHON27, IS_NOT_LINUX)
NOT_CPYTHON27_OR_LINUX = "%s or %s" % (NOT_CPYTHON27, IS_LINUX)
NOT_CPYTHON36_OR_LINUX = "%s or %s" % (NOT_CPYTHON36, IS_LINUX)


@contextlib.contextmanager
def temporary_dir():
  td = tempfile.mkdtemp()
  try:
    yield td
  finally:
    safe_rmtree(td)


@contextlib.contextmanager
def temporary_filename():
  """Creates a temporary filename.

  This is useful when you need to pass a filename to an API. Windows requires all
  handles to a file be closed before deleting/renaming it, so this makes it a bit
  simpler."""
  with named_temporary_file() as fp:
    fp.write(b'')
    fp.close()
    yield fp.name


def random_bytes(length):
  return ''.join(
      map(chr, (random.randint(ord('a'), ord('z')) for _ in range(length)))).encode('utf-8')


def get_dep_dist_names_from_pex(pex_path, match_prefix=''):
  """Given an on-disk pex, extract all of the unique first-level paths under `.deps`."""
  with open_zip(pex_path) as pex_zip:
    dep_gen = (f.split(os.sep)[1] for f in pex_zip.namelist() if f.startswith('.deps/'))
    return set(item for item in dep_gen if item.startswith(match_prefix))


@contextlib.contextmanager
def temporary_content(content_map, interp=None, seed=31337, perms=0o644):
  """Write content to disk where content is map from string => (int, string).

     If target is int, write int random bytes.  Otherwise write contents of string."""
  random.seed(seed)
  interp = interp or {}
  with temporary_dir() as td:
    for filename, size_or_content in content_map.items():
      dest = os.path.join(td, filename)
      safe_mkdir(os.path.dirname(dest))
      with open(dest, 'wb') as fp:
        if isinstance(size_or_content, int):
          fp.write(random_bytes(size_or_content))
        else:
          fp.write((size_or_content % interp).encode('utf-8'))
      os.chmod(dest, perms)
    yield td


def yield_files(directory):
  for root, _, files in os.walk(directory):
    for f in files:
      filename = os.path.join(root, f)
      rel_filename = os.path.relpath(filename, directory)
      yield filename, rel_filename


def write_zipfile(directory, dest, reverse=False):
  with open_zip(dest, 'w') as zf:
    for filename, rel_filename in sorted(yield_files(directory), reverse=reverse):
      zf.write(filename, arcname=rel_filename)
  return dest


@contextlib.contextmanager
def make_project(name='my_project',
                 version='0.0.0',
                 zip_safe=True,
                 install_reqs=None,
                 extras_require=None):

  project_content = {
    'setup.py': dedent('''
      from setuptools import setup

      setup(
        name=%(project_name)r,
        version=%(version)r,
        zip_safe=%(zip_safe)r,
        packages=[%(project_name)r],
        scripts=[
          'scripts/hello_world',
          'scripts/shell_script',
        ],
        package_data={%(project_name)r: ['package_data/*.dat']},
        install_requires=%(install_requires)r,
        extras_require=%(extras_require)r,
      )
    '''),
    'scripts/hello_world': '#!/usr/bin/env python\nprint("hello world!")\n',
    'scripts/shell_script': '#!/usr/bin/env bash\necho hello world\n',
    os.path.join(name, '__init__.py'): 0,
    os.path.join(name, 'my_module.py'): 'def do_something():\n  print("hello world!")\n',
    os.path.join(name, 'package_data/resource1.dat'): 1000,
    os.path.join(name, 'package_data/resource2.dat'): 1000,
  }

  interp = {'project_name': name,
            'version': version,
            'zip_safe': zip_safe,
            'install_requires': install_reqs or [],
            'extras_require': extras_require or {}}

  with temporary_content(project_content, interp=interp) as td:
    yield td


@contextlib.contextmanager
def make_installer(name='my_project',
                   version='0.0.0',
                   installer_impl=EggInstaller,
                   zip_safe=True,
                   install_reqs=None,
                   extras_require=None,
                   interpreter=None,
                   **kwargs):

  with make_project(name=name,
                    version=version,
                    zip_safe=zip_safe,
                    install_reqs=install_reqs,
                    extras_require=extras_require) as td:
    yield installer_impl(td, interpreter=interpreter, **kwargs)


@contextlib.contextmanager
def make_source_dir(name='my_project', version='0.0.0', install_reqs=None, extras_require=None):
  with make_project(name=name,
                    version=version,
                    install_reqs=install_reqs,
                    extras_require=extras_require) as td:
    yield td


def make_sdist(name='my_project',
               version='0.0.0',
               zip_safe=True,
               install_reqs=None,
               extras_require=None):
  with make_installer(name=name,
                      version=version,
                      installer_impl=Packager,
                      zip_safe=zip_safe,
                      install_reqs=install_reqs,
                      extras_require=extras_require) as packager:
    return packager.sdist()


@contextlib.contextmanager
def make_bdist(name='my_project', version='0.0.0', installer_impl=EggInstaller, zipped=False,
               zip_safe=True, **kwargs):
  with make_installer(name=name,
                      version=version,
                      installer_impl=installer_impl,
                      zip_safe=zip_safe,
                      **kwargs) as installer:
    dist_location = installer.bdist()
    if zipped:
      yield DistributionHelper.distribution_from_path(dist_location)
    else:
      with temporary_dir() as td:
        extract_path = os.path.join(td, os.path.basename(dist_location))
        with open_zip(dist_location) as zf:
          zf.extractall(extract_path)
        yield DistributionHelper.distribution_from_path(extract_path)


COVERAGE_PREAMBLE = """
try:
  from coverage import coverage
  cov = coverage(auto_data=True, data_suffix=True)
  cov.start()
except ImportError:
  pass
"""


def write_simple_pex(td,
                     exe_contents=None,
                     dists=None,
                     sources=None,
                     coverage=False,
                     interpreter=None):
  """Write a pex file that optionally contains an executable entry point.

  :param str td: temporary directory path
  :param str exe_contents: entry point python file
  :param dists: distributions to include, typically sdists or bdists
  :type: list of :class:`pex.third_party.pkg_resources.Distribution`
  :param sources: sources to include, as a list of pairs (env_filename, contents)
  :type sources: list of (str, str)
  :param bool coverage: include coverage header
  :param interpreter: a custom interpreter to use to build the pex
  :type interpreter: :class:`pex.interpreter.PythonInterpreter`
  """
  dists = dists or []
  sources = sources or []

  safe_mkdir(td)

  pb = PEXBuilder(path=td,
                  preamble=COVERAGE_PREAMBLE if coverage else None,
                  interpreter=interpreter)

  for dist in dists:
    pb.add_dist_location(dist.location)

  for env_filename, contents in sources:
    src_path = os.path.join(td, env_filename)
    safe_mkdir(os.path.dirname(src_path))
    with open(src_path, 'w') as fp:
      fp.write(contents)
    pb.add_source(src_path, env_filename)

  if exe_contents:
    with open(os.path.join(td, 'exe.py'), 'w') as fp:
      fp.write(exe_contents)
    pb.set_executable(os.path.join(td, 'exe.py'))

  pb.freeze()

  return pb


class IntegResults(namedtuple('results', ['output', 'error', 'return_code'])):
  """Convenience object to return integration run results."""

  def assert_success(self):
    assert self.return_code == 0, ('integration test failed: return_code={}, output={}, error={}'
                                   .format(self.return_code, self.output, self.error))

  def assert_failure(self):
    assert self.return_code != 0


def run_pex_command(args, env=None, python=sys.executable):
  """Simulate running pex command for integration testing.

  This is different from run_simple_pex in that it calls the pex command rather
  than running a generated pex.  This is useful for testing end to end runs
  with specific command line arguments or env options.
  """
  cmd = [python, '-mpex', '-vvvvv'] + list(args)
  process = Executor.open_process(cmd=cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  output, error = process.communicate()
  return IntegResults(output.decode('utf-8'), error.decode('utf-8'), process.returncode)


def run_simple_pex(pex, args=(), interpreter=None, stdin=None, **kwargs):
  p = PEX(pex, interpreter=interpreter)
  process = p.run(args=args,
                  blocking=False,
                  stdin=subprocess.PIPE,
                  stdout=subprocess.PIPE,
                  stderr=subprocess.STDOUT,
                  **kwargs)
  stdout, _ = process.communicate(input=stdin)
  print(stdout.decode('utf-8') if PY3 else stdout)
  return stdout.replace(b'\r', b''), process.returncode


def run_simple_pex_test(body, args=(), env=None, dists=None, coverage=False, interpreter=None):
  with nested(temporary_dir(), temporary_dir()) as (td1, td2):
    pb = write_simple_pex(td1, body, dists=dists, coverage=coverage, interpreter=interpreter)
    pex = os.path.join(td2, 'app.pex')
    pb.build(pex)
    return run_simple_pex(pex, args=args, env=env, interpreter=interpreter)


def bootstrap_python_installer(dest):
  safe_rmtree(dest)
  for _ in range(3):
    try:
      subprocess.check_call(
        ['git', 'clone', 'https://github.com/pyenv/pyenv.git', dest]
      )
    except subprocess.CalledProcessError as e:
      print('caught exception: %r' % e)
      continue
    else:
      break
  else:
    raise RuntimeError("Helper method could not clone pyenv from git after 3 tries")
  # Create an empty file indicating the fingerprint of the correct set of test interpreters.
  touch(os.path.join(dest, _INTERPRETER_SET_FINGERPRINT))


# NB: We keep the pool of bootstrapped interpreters as small as possible to avoid timeouts in CI
# otherwise encountered when fetching and building too many on a cache miss. In the past we had
# issues with the combination of 7 total unique interpreter versions and a Travis-CI timeout of 50
# minutes for a shard.
PY27 = '2.7.15'
PY35 = '3.5.6'
PY36 = '3.6.6'

_VERSIONS = (PY27, PY35, PY36)
# This is the filename of a sentinel file that sits in the pyenv root directory.
# Its purpose is to indicate whether pyenv has the correct interpreters installed
# and will be useful for indicating whether we should trigger a reclone to update
# pyenv.
_INTERPRETER_SET_FINGERPRINT = '_'.join(_VERSIONS) + '_pex_fingerprint'


def ensure_python_distribution(version):
  if version not in _VERSIONS:
    raise ValueError('Please constrain version to one of {}'.format(_VERSIONS))

  pyenv_root = os.path.join(os.getcwd(), '.pyenv_test')
  interpreter_location = os.path.join(pyenv_root, 'versions', version)
  pyenv = os.path.join(pyenv_root, 'bin', 'pyenv')
  pip = os.path.join(interpreter_location, 'bin', 'pip')

  if not os.path.exists(os.path.join(pyenv_root, _INTERPRETER_SET_FINGERPRINT)):
    bootstrap_python_installer(pyenv_root)

  if not os.path.exists(interpreter_location):
    env = os.environ.copy()
    env['PYENV_ROOT'] = pyenv_root
    if sys.platform.lower() == 'linux':
      env['CONFIGURE_OPTS'] = '--enable-shared'
    subprocess.check_call([pyenv, 'install', '--keep', version], env=env)
    subprocess.check_call([pip, 'install', '-U', 'pip'])

  python = os.path.join(interpreter_location, 'bin', 'python' + version[0:3])
  return python, pip


def ensure_python_interpreter(version):
  python, _ = ensure_python_distribution(version)
  return python
