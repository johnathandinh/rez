from __future__ import print_function

from rez.packages_ import get_latest_package
from rez.vendor.version.version import Version, VersionError
from rez.vendor.distlib import DistlibException
from rez.vendor.distlib.database import DistributionPath
from rez.vendor.distlib.markers import interpret
from rez.vendor.distlib.util import parse_name_and_version
from rez.vendor.enum.enum import Enum
from rez.resolved_context import ResolvedContext
from rez.utils.system import popen
from rez.utils.logging_ import print_debug, print_info, print_warning
from rez.exceptions import BuildError, PackageFamilyNotFoundError, \
    PackageNotFoundError, convert_errors
from rez.package_maker__ import make_package
from rez.config import config
from rez.system import System
from tempfile import mkdtemp
from StringIO import StringIO
from pipes import quote
import subprocess
import os.path
import shutil
import sys
import os
import re


VERSION_PATTERN = r"""
    v?
    (?:
        (?:(?P<epoch>[0-9]+)!)?                           # epoch
        (?P<release>[0-9]+(?:\.[0-9]+)*)                  # release segment
        (?P<pre>                                          # pre-release
            [-_\.]?
            (?P<pre_l>(a|b|c|rc|alpha|beta|pre|preview))
            [-_\.]?
            (?P<pre_n>[0-9]+)?
        )?
        (?P<post>                                         # post release
            (?:-(?P<post_n1>[0-9]+))
            |
            (?:
                [-_\.]?
                (?P<post_l>post|rev|r)
                [-_\.]?
                (?P<post_n2>[0-9]+)?
            )
        )?
        (?P<dev>                                          # dev release
            [-_\.]?
            (?P<dev_l>dev)
            [-_\.]?
            (?P<dev_n>[0-9]+)?
        )?
    )
    (?:\+(?P<local>[a-z0-9]+(?:[-_\.][a-z0-9]+)*))?       # local version
"""

CANONICAL_VERSION_RE = re.compile(
    r"^\s*" + VERSION_PATTERN + r"\s*$",
    re.VERBOSE | re.IGNORECASE,
)


class InstallMode(Enum):
    # don't install dependencies. Build may fail, for example the package may
    # need to compile against a dependency. Will work for pure python though.
    no_deps = 0
    # only install dependencies that we have to. If an existing rez package
    # satisfies a dependency already, it will be used instead. The default.
    min_deps = 1
    # install dependencies even if an existing rez package satisfies the
    # dependency, if the dependency is newer.
    new_deps = 2
    # install dependencies even if a rez package of the same version is already
    # available, if possible. For example, if you are performing a local install,
    # a released (central) package may match a dependency; but with this mode
    # enabled, a new local package of the same version will be installed as well.
    #
    # Typically, if performing a central install with the rez-pip --release flag,
    # max_deps is equivalent to new_deps.
    max_deps = 3


def _get_dependencies(requirement, distributions):
    def get_distribution_name(pip_name):
        pip_to_rez_name = pip_name.lower().replace("-", "_")
        for dist in distributions:
            _name, _ = parse_name_and_version(dist.name_and_version)
            if _name.replace("-", "_") == pip_to_rez_name:
                return dist.name.replace("-", "_")

    result = []
    requirements = ([requirement] if isinstance(requirement, basestring)
                    else requirement["requires"])

    for package in requirements:
        if "(" in package:
            try:
                name, version = parse_name_and_version(package)
                version = version.replace("==", "")
                name = get_distribution_name(name)
            except DistlibException:
                n, vs = package.split(' (')
                vs = vs[:-1]
                versions = []
                for v in vs.split(','):
                    package = "%s (%s)" % (n, v)
                    name, version = parse_name_and_version(package)
                    version = version.replace("==", "")
                    versions.append(version)
                version = "".join(versions)

            name = get_distribution_name(name)
            result.append("-".join([name, version]))
        else:
            name = get_distribution_name(package)
            result.append(name)

    return result


def is_exe(fpath):
        return os.path.exists(fpath) and os.access(fpath, os.X_OK)


def pip_to_rez_version(dist_version):
    """Convert a distribution version to a rez compatible version.

    The python version schema specification isn't 100% compatible with rez.

    1: version epochs (they make no sense to rez, so they'd just get stripped
       of the leading N!;
    2: python versions are case insensitive, so they should probably be
       lowercased when converted to a rez version.
    3: local versions are also not compatible with rez

    The canonical public version identifiers MUST comply with the following scheme:
    [N!]N(.N)*[{a|b|rc}N][.postN][.devN]

    Epoch segment: N! - skip
    Release segment: N(.N)* 0 as is
    Pre-release segment: {a|b|rc}N - always lowercase
    Post-release segment: .postN - always lowercase
    Development release segment: .devN - always lowercase

    Local version identifiers MUST comply with the following scheme:
    <public version identifier>[+<local version label>] - use - instead of +

    Arguments:
        dist_version (str): The distribution version to be converted.
    """
    version_match = CANONICAL_VERSION_RE.match(dist_version)
    version_segments = version_match.groupdict()

    available_segments = dict((k, v) for k, v in version_segments.iteritems() if v)
    version = ""
    if "release" in available_segments:
        release = available_segments["release"]
        version += release
        if "pre" in available_segments:
            pre = available_segments["pre"].lower()
            version += pre
        if "post" in available_segments:
            post = available_segments["post"].lower()
            version += post
        if "dev" in available_segments:
            dev = available_segments["dev"].lower()
            version += dev
        if "local" in available_segments:
            local = available_segments["local"]
            version += "-" + local

    return version


def pip_to_rez_package_name(distribution):
    """Convert a distribution name to a rez compatible name.

    The rez package name can't be simply set to the dist name, because some
    pip packages have hyphen in the name. In rez this is not a valid package
    name (it would be interpreted as the start of the version).

    Example: my-pkg-1.2 is 'my', version 'pkg-1.2'.

    Arguments:
        distribution (Distribution): The distribution whose name to convert.
    """
    name, _ = parse_name_and_version(distribution.name_and_version)
    name = distribution.name[0:len(name)].replace("-", "_")
    return name


def run_pip_command(command_args, pip_version=None, python_version=None):
    """Run a pip command.

    Args:
        command_args (list of str): Args to pip.

    Returns:
        `subprocess.Popen`: Pip process.
    """
    pip_exe, context = find_pip(pip_version, python_version)
    command = [pip_exe] + list(command_args)

    if context is None:
        return popen(command)
    else:
        return context.execute_shell(command=command, block=False)


def find_pip(pip_version=None, python_version=None):
    """Find a pip exe using the given python version.

    Returns:
        2-tuple:
            str: pip executable;
            `ResolvedContext`: Context containing pip, or None if we fell back
                to system pip.
    """
    pip_exe = "pip"

    try:
        context = create_context(pip_version, python_version)
    except BuildError:
        # fall back on system pip. Not ideal but at least it's something
        from rez.backport.shutilwhich import which

        pip_exe = which("pip")

        if pip_exe:
            print_warning(
                "pip rez package could not be found; system 'pip' command (%s) "
                "will be used instead." % pip_exe)
            context = None
        else:
            raise

    # check pip version, must be >=19 to support PEP517
    try:
        pattern = r"pip\s(?P<ver>\d+\.*\d*\.*\d*)"
        ver_str = subprocess.check_output([pip_exe, '-V'])
        match = re.search(pattern, ver_str)
        ver = match.group('ver')
        pip_major = ver.split('.')[0]

        if int(pip_major) < 19:
            raise VersionError("pip >= 19 is required! Please update your pip.")
    except:
        # silently skip if pip version detection failed, pip itself will show
        # a reasonable error message at the least.
        pass

    return pip_exe, context


def create_context(pip_version=None, python_version=None):
    """Create a context containing the specific pip and python.

    Args:
        pip_version (str or `Version`): Version of pip to use, or latest if None.
        python_version (str or `Version`): Python version to use, or latest if
            None.

    Returns:
        `ResolvedContext`: Context containing pip and python.
    """
    # determine pip pkg to use for install, and python variants to install on
    if pip_version:
        pip_req = "pip-%s" % str(pip_version)
    else:
        pip_req = "pip"

    if python_version:
        ver = Version(str(python_version))
        major_minor_ver = ver.trim(2)
        py_req = "python-%s" % str(major_minor_ver)
    else:
        # use latest major.minor
        package = get_latest_package("python")
        if package:
            major_minor_ver = package.version.trim(2)
        else:
            # no python package. We're gonna fail, let's just choose current
            # python version (and fail at context creation time)
            major_minor_ver = '.'.join(map(str, sys.version_info[:2]))

        py_req = "python-%s" % str(major_minor_ver)

    # use pip + latest python to perform pip download operations
    request = [pip_req, py_req]

    with convert_errors(from_=(PackageFamilyNotFoundError, PackageNotFoundError),
                        to=BuildError, msg="Cannot run - pip or python rez "
                        "package is not present"):
        context = ResolvedContext(request)

    # print pip package used to perform the install
    pip_variant = context.get_resolved_package("pip")
    pip_package = pip_variant.parent
    print_info("Using %s (%s)" % (pip_package.qualified_name, pip_variant.uri))

    return context


def pip_install_package(source_name, pip_version=None, python_version=None,
                        mode=InstallMode.min_deps, release=False):
    """Install a pip-compatible python package as a rez package.
    Args:
        source_name (str): Name of package or archive/url containing the pip
            package source. This is the same as the arg you would pass to
            the 'pip install' command.
        pip_version (str or `Version`): Version of pip to use to perform the
            install, uses latest if None.
        python_version (str or `Version`): Python version to use to perform the
            install, and subsequently have the resulting rez package depend on.
        mode (`InstallMode`): Installation mode, determines how dependencies are
            managed.
        release (bool): If True, install as a released package; otherwise, it
            will be installed as a local package.

    Returns:
        2-tuple:
            List of `Variant`: Installed variants;
            List of `Variant`: Skipped variants (already installed).
    """
    installed_variants = []
    skipped_variants = []

    pip_exe, context = find_pip(pip_version, python_version)

    # TODO: should check if packages_path is writable before continuing with pip
    #
    packages_path = (config.release_packages_path if release
                     else config.local_packages_path)

    tmpdir = mkdtemp(suffix="-rez", prefix="pip-")
    stagingdir = os.path.join(tmpdir, "rez_staging")
    stagingsep = "".join([os.path.sep, "rez_staging", os.path.sep])

    destpath = os.path.join(stagingdir, "python")
    # TODO use binpath once https://github.com/pypa/pip/pull/3934 is approved
    binpath = os.path.join(stagingdir, "bin")

    if context and config.debug("package_release"):
        buf = StringIO()
        print("\n\npackage download environment:", file=buf)
        context.print_info(buf)
        _log(buf.getvalue())

    # Build pip commandline
    cmd = [
        pip_exe, "install",
        "--use-pep517",
        "--target=%s" % destpath
    ]

    if mode == InstallMode.no_deps:
        cmd.append("--no-deps")
    cmd.append(source_name)

    _cmd(context=context, command=cmd)
    _system = System()

    # Collect resulting python packages using distlib
    distribution_path = DistributionPath([destpath])
    distributions = [d for d in distribution_path.get_distributions()]

    # moving bin folder to expected relative location as per wheel RECORD files
    staged_binpath = os.path.join(destpath, "bin")
    if os.path.isdir(staged_binpath):
        shutil.move(os.path.join(destpath, "bin"), binpath)

    for distribution in distribution_path.get_distributions():
        requirements = []
        if distribution.metadata.run_requires:
            # Handle requirements. Currently handles conditional environment based
            # requirements and normal requirements
            # TODO: Handle optional requirements?
            for requirement in distribution.metadata.run_requires:
                if "environment" in requirement:
                    if interpret(requirement["environment"]):
                        requirements.extend(_get_dependencies(requirement, distributions))
                elif "extra" in requirement:
                    # Currently ignoring optional requirements
                    pass
                else:
                    requirements.extend(_get_dependencies(requirement, distributions))

        tools = []
        src_dst_lut = {}

        for installed_file in distribution.list_installed_files():
            # distlib expects the script files to be located in ../../bin/
            # when in fact ../bin seems to be the resulting path after the
            # installation as such we need to point the bin files to the
            # expected location to match wheel RECORD files
            installed_filepath = os.path.normpath(installed_file[0])
            bin_prefix = os.path.join('..', '..', 'bin') + os.sep

            if installed_filepath.startswith(bin_prefix):
                # account for extra parentdir as explained above
                installed = os.path.join(destpath, '_', installed_filepath)
            else:
                installed = os.path.join(destpath, installed_filepath)

            source_file = os.path.normpath(installed)

            if os.path.exists(source_file):
                destination_file = os.path.relpath(source_file, stagingdir)
                exe = False

                if is_exe(source_file):
                    _file = os.path.basename(destination_file)
                    tools.append(_file)
                    exe = True

                data = [destination_file, exe]
                src_dst_lut[source_file] = data
            else:
                _log("Source file does not exist: " + source_file + "!")

        def make_root(variant, path):
            """Using distlib to iterate over all installed files of the current
            distribution to copy files to the target directory of the rez package
            variant
            """
            for source_file, data in src_dst_lut.items():
                destination_file, exe = data
                destination_file = os.path.normpath(os.path.join(path, destination_file))

                if not os.path.exists(os.path.dirname(destination_file)):
                    os.makedirs(os.path.dirname(destination_file))

                shutil.copyfile(source_file, destination_file)
                if exe:
                    shutil.copystat(source_file, destination_file)

        # determine variant requirements
        # TODO detect if platform/arch/os necessary, no if pure python
        variant_reqs = []
        variant_reqs.append("platform-%s" % _system.platform)
        variant_reqs.append("arch-%s" % _system.arch)
        variant_reqs.append("os-%s" % _system.os)

        if context is None:
            # since we had to use system pip, we have to assume system python version
            py_ver = '.'.join(map(str, sys.version_info[:2]))
        else:
            python_variant = context.get_resolved_package("python")
            py_ver = python_variant.version.trim(2)

        variant_reqs.append("python-%s" % py_ver)

        name = pip_to_rez_package_name(distribution)

        with make_package(name, packages_path, make_root=make_root) as pkg:
            pkg.version = pip_to_rez_version(distribution.version)
            if distribution.metadata.summary:
                pkg.description = distribution.metadata.summary

            pkg.variants = [variant_reqs]
            if requirements:
                pkg.requires = requirements

            commands = []
            commands.append("env.PYTHONPATH.append('{root}/python')")

            if tools:
                pkg.tools = tools
                commands.append("env.PATH.append('{root}/bin')")

            pkg.commands = '\n'.join(commands)

        installed_variants.extend(pkg.installed_variants or [])
        skipped_variants.extend(pkg.skipped_variants or [])

    # cleanup
    shutil.rmtree(tmpdir)

    return installed_variants, skipped_variants


def _cmd(context, command):
    cmd_str = ' '.join(quote(x) for x in command)
    _log("running: %s" % cmd_str)

    if context is None:
        p = popen(command)
    else:
        p = context.execute_shell(command=command, block=False)

    p.wait()

    if p.returncode:
        raise BuildError("Failed to download source with pip: %s" % cmd_str)


_verbose = config.debug("package_release")


def _log(msg):
    if _verbose:
        print_debug(msg)


# Copyright 2013-2016 Allan Johns.
#
# This library is free software: you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation, either
# version 3 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library.  If not, see <http://www.gnu.org/licenses/>.
