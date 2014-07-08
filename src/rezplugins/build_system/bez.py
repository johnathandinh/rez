"""
Built-in simple python build system.
"""
from rez.vendor import yaml
from rez.build_system import BuildSystem
from rez.exceptions import BuildSystemError
from rez.util import create_forwarding_script
from rez.resolved_context import ResolvedContext
from rez.packages import Package
from rez.config import config
import os.path
import sys



class BezBuildSystem(BuildSystem):
    """The Bez build system.

    Bez is a simple build system, which runs the 'bez' binary in the build
    environment. All bez does is run the file 'rezbuild.py' (your package's
    build file) in a python subprocess. The code in rezbuild.py has access to
    any python module dependencies of the project.

    Unless told otherwise, Bez expects to find a 'python' executable in the
    build environment. If you have a specific interpreter you want to use, it
    needs to be available as a rez python package, and you should list it as a
    private build requirement in your package.

    The function signature of 'build' is as follows:

    build(source_path, build_path, install_path, targets):
        Args:
            source_path (str): Path containing the project;
            build_path (str): Path to store build files;
            install_path (str): Path to install targets to, if install is True;
            targets (list of str): List of targets to build, assume build all if
                None. Your build code should recognise 'install' as a special
                build target, meaning to install the entire build.
    """
    @classmethod
    def name(cls):
        return "bez"

    @classmethod
    def is_valid_root(cls, path):
        return os.path.isfile(os.path.join(path, "rezbuild.py"))

    def __init__(self, working_dir, opts=None, write_build_scripts=False,
                 verbose=False, build_args=[], child_build_args=[]):
        super(BezBuildSystem, self).__init__(working_dir,
                                             opts=opts,
                                             write_build_scripts=write_build_scripts,
                                             verbose=verbose,
                                             build_args=build_args,
                                             child_build_args=child_build_args)

    def build(self, context, build_path, install_path, install=False):
        # communicate args to bez by placing in a file
        doc = dict(
            source_path=self.working_dir,
            build_path=build_path,
            install_path=install_path)

        ret = {}
        content = yaml.dump(doc)
        bezfile = os.path.join(build_path, ".bez.yaml")
        with open(bezfile, 'w') as f:
            f.write(content + '\n')

        if self.write_build_scripts:
            # write out the script that places the user in a build env, where
            # they can run bez directly themselves.
            build_env_script = os.path.join(build_path, "build-env")
            create_forwarding_script(build_env_script,
                                     module=("build_system", "bez"),
                                     func_name="_FWD__spawn_build_shell",
                                     working_dir=self.working_dir,
                                     build_dir=build_path)
            ret["success"] = True
            ret["build_env_script"] = build_env_script
            return ret

        # run bez in the build environment
        cmd = ["bez"]
        if install and "install" not in cmd:
            cmd.append("install")

        retcode,_,_ = context.execute_shell(command=cmd,
                                            block=True,
                                            cwd=build_path)
        ret["success"] = (not retcode)
        return ret


def _FWD__spawn_build_shell(working_dir, build_dir):
    # This spawns a shell that the user can run 'bez' in directly
    context = ResolvedContext.load(os.path.join(build_dir, "build.rxt"))
    package = Package(working_dir)
    config.override("prompt", "BUILD>")

    retcode,_,_ = context.execute_shell(block=True,
                                       cwd=build_dir)
    sys.exit(retcode)


def register_plugin():
    return BezBuildSystem