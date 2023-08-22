#!/usr/bin/env python3
"""Script to add non-"latest" miniconda releases.
Written for python 3.7.

Checks the miniconda download archives for new versions,
then writes a build script for any which do not exist locally,
saving it to plugins/python-build/share/python-build.

Ignores releases below 4.3.30.
Also ignores sub-patch releases if that major.minor.patch already exists,
but otherwise, takes the latest sub-patch release for given OS/arch.
Assumes all miniconda3 releases < 4.7 default to python 3.6, and anything else 3.7.
"""

import textwrap
from argparse import ArgumentParser
from collections import defaultdict
from enum import Enum
from functools import total_ordering
from pathlib import Path
from typing import NamedTuple, List, Optional, DefaultDict, Dict
import logging
import string

import requests_html

logger = logging.getLogger(__name__)

CONDA_REPO = "https://repo.anaconda.com"
MINICONDA_REPO = f"{CONDA_REPO}/miniconda"
ANACONDA_REPO = f"{CONDA_REPO}/archive"

install_script_fmt = """
case "$(anaconda_architecture 2>/dev/null || true)" in
{install_lines}
* )
  {{ echo
    colorize 1 "ERROR"
    echo ": The binary distribution of {tflavor} is not available for $(anaconda_architecture 2>/dev/null || true)."
    echo
  }} >&2
  exit 1
  ;;
esac
""".lstrip()

install_line_fmt = """
"{os}-{arch}" )
  install_script "{tflavor}{suffix}-{version_py_version}{version_str}-{os}-{arch}" "{repo}/{tflavor}{suffix}-{version_py_version}{version_str}-{os}-{arch}.sh#{md5}" "{flavor}" verify_{py_version}
  ;;
""".strip()

here = Path(__file__).resolve()
out_dir: Path = here.parent.parent / "share" / "python-build"


class StrEnum(str, Enum):
    """Enum subclass whose members are also instances of str
    and directly comparable to strings. str type is forced at declaration.

    Adapted from https://github.com/kissgyorgy/enum34-custom/blob/dbc89596761c970398701d26c6a5bbcfcf70f548/enum_custom.py#L100
    (MIT license)
    """

    def __new__(cls, *args):
        for arg in args:
            if not isinstance(arg, str):
                raise TypeError(f"Not text {arg}:")

        return super(StrEnum, cls).__new__(cls, *args)

    def __str__(self):
        return str(self.value)


class SupportedOS(StrEnum):
    LINUX = "Linux"
    MACOSX = "MacOSX"


class SupportedArch(StrEnum):
    AARCH64 = "aarch64"
    ARM64 = "arm64"
    PPC64LE = "ppc64le"
    S390X = "s390x"
    X86_64 = "x86_64"
    X86 = "x86"


class Flavor(StrEnum):
    ANACONDA = "anaconda"
    MINICONDA = "miniconda"

    
class TFlavor(StrEnum):
    ANACONDA = "Anaconda"
    MINICONDA = "Miniconda"


class Suffix(StrEnum):
    TWO = "2"
    THREE = "3"
    NONE = ""


class PyVersion(StrEnum):
    PY27 = "py27"
    PY36 = "py36"
    PY37 = "py37"
    PY38 = "py38"
    PY39 = "py39"

    def version(self):
        first, *others = self.value[2:]
        return f"{first}.{''.join(others)}"

    def version_info(self):
        return tuple(int(n) for n in self.version().split("."))


@total_ordering
class VersionStr(str):
    def info(self):
        return tuple(int(n) for n in self.split("."))

    def __eq__(self, other):
        return str(self) == str(other)

    def __lt__(self, other):
        if isinstance(other, VersionStr):
            return self.info() < other.info()
        raise ValueError("VersionStr can only be compared to other VersionStr")

    @classmethod
    def from_info(cls, version_info):
        return VersionStr(".".join(str(n) for n in version_info))

    def __hash__(self):
        return hash(str(self))


class CondaVersion(NamedTuple):
    flavor: Flavor
    suffix: Suffix
    version_str: VersionStr
    py_version: Optional[PyVersion]

    @classmethod
    def from_str(cls, s):
        """
        Convert a string of the form "miniconda_n-ver" or "miniconda_n-py_ver-ver" to a :class:`CondaVersion` object.
        """
        components = s.split("-")
        if len(components) == 3:
            miniconda_n, py_ver, ver = components
            py_ver = PyVersion(f"py{py_ver.replace('.', '')}")
        else:
            miniconda_n, ver = components
            py_ver = None

        suffix = miniconda_n[-1]
        if suffix in string.digits:
            flavor = miniconda_n[:-1]
        else:
            flavor = miniconda_n
            suffix = ""
        return CondaVersion(Flavor(flavor), Suffix(suffix), VersionStr(ver), py_ver)

    def to_filename(self):
        if self.py_version:
            return f"{self.flavor}{self.suffix}-{self.py_version.version()}-{self.version_str}"
        else:
            return f"{self.flavor}{self.suffix}-{self.version_str}"

    def default_py_version(self):
        """
        :class:`PyVersion` of Python used with this Miniconda version
        """
        if self.py_version:
            return self.py_version
        elif self.suffix == Suffix.TWO:
            return PyVersion.PY27

        v = self.version_str.info()
        if self.flavor == "anaconda":
            # https://docs.anaconda.com/anaconda/reference/release-notes/
            if v >= (2021,11):
                return PyVersion.PY39
            if v >= (2020,7):
                return PyVersion.PY38
            if v >= (2020,2):
                return PyVersion.PY37
            return PyVersion.PY37 if v >= (5,3,0) else PyVersion.PY36
        elif self.flavor == "miniconda":
            # https://docs.conda.io/projects/conda/en/latest/user-guide/tasks/manage-python.html
            return PyVersion.PY36 if v < (4, 7) else PyVersion.PY37
        raise ValueError(flavor)


class CondaSpec(NamedTuple):
    tflavor: TFlavor
    version: CondaVersion
    os: SupportedOS
    arch: SupportedArch
    md5: str
    repo: str
    py_version: Optional[PyVersion] = None

    @classmethod
    def from_filestem(cls, stem, md5, repo, py_version=None):
        miniconda_n, ver, os, arch = stem.split("-")
        suffix = miniconda_n[-1]
        if suffix in string.digits:
            tflavor = miniconda_n[:-1]
        else:
            tflavor = miniconda_n
            suffix = ""
        flavor = tflavor.lower()

        if ver.startswith("py"):
            py_ver, ver = ver.split("_", maxsplit=1)
            py_ver = PyVersion(py_ver)
        else:
            py_ver = None
        spec = CondaSpec(
            TFlavor(tflavor),
            CondaVersion(Flavor(flavor), Suffix(suffix), VersionStr(ver), py_ver),
            SupportedOS(os),
            SupportedArch(arch),
            md5,
            repo,
        )
        if py_version is None:
            spec = spec.with_py_version(spec.version.default_py_version())
        return spec

    def to_install_lines(self):
        """
        Installation command for this version of Miniconda for use in a Pyenv installation script
        """
        return install_line_fmt.format(
            tflavor=self.tflavor,
            flavor=self.version.flavor,
            repo=self.repo,
            suffix=self.version.suffix,
            version_str=self.version.version_str,
            version_py_version=f"{self.version.py_version}_" if self.version.py_version else "",
            os=self.os,
            arch=self.arch,
            md5=self.md5,
            py_version=self.py_version,
        )

    def with_py_version(self, py_version: PyVersion):
        return CondaSpec(*self[:-1], py_version=py_version)


def make_script(specs: List[CondaSpec]):
    install_lines = [s.to_install_lines() for s in specs]
    return install_script_fmt.format(
        install_lines="\n".join(install_lines),
        tflavor=specs[0].tflavor,
    )


def get_existing_condas(name):
    """
    Enumerate existing Miniconda installation scripts in share/python-build/ except rolling releases.

    :returns: A generator of :class:`CondaVersion` objects.
    """
    logger.info("Getting known %(name)s versions",locals())
    for p in out_dir.iterdir():
        entry_name = p.name
        if not p.is_file() or not entry_name.startswith(name):
            continue
        try:
            v = CondaVersion.from_str(entry_name)
            if v.version_str != "latest":
                logger.debug("Found existing %(name)s version %(v)s", locals())
                yield v
        except ValueError:
            pass


def get_available_condas(name, repo):
    """
    Fetch remote miniconda versions.

    :returns: A generator of :class:`CondaSpec` objects for each release available for download
    except rolling releases.
    """
    logger.info("Fetching remote %(name)s versions",locals())
    session = requests_html.HTMLSession()
    response = session.get(repo)
    page: requests_html.HTML = response.html
    table = page.find("table", first=True)
    rows = table.find("tr")[1:]
    for row in rows:
        f, size, date, md5 = row.find("td")
        fname = f.text
        md5 = md5.text

        if not fname.endswith(".sh"):
            continue
        stem = fname[:-3]

        try:
            s = CondaSpec.from_filestem(stem, md5, repo)
            if s.version.version_str != "latest":
                logger.debug("Found remote %(name)s version %(s)s", locals())
                yield s
        except ValueError:
            pass


def key_fn(spec: CondaSpec):
    return (
        spec.tflavor,
        spec.version.version_str.info(),
        spec.version.suffix.value,
        spec.os.value,
        spec.arch.value,
    )


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "-d", "--dry-run", action="store_true",
        help="Do not write scripts, just report them to stdout",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Increase verbosity of logging",
    )
    parsed = parser.parse_args()

    log_level = {
        0: logging.WARNING,
        1: logging.INFO,
        2: logging.DEBUG,
    }.get(parsed.verbose, logging.DEBUG)
    logging.basicConfig(level=log_level)
    if parsed.verbose < 3:
        logging.getLogger("requests").setLevel(logging.WARNING)

    existing_versions = set()
    available_specs = set()
    for name,repo in ("miniconda",MINICONDA_REPO),("anaconda",ANACONDA_REPO):
        existing_versions |= set(get_existing_condas(name))
        available_specs |= set(get_available_condas(name, repo))

    # version triple to triple-ified spec to raw spec
    to_add: DefaultDict[
        CondaVersion, Dict[CondaSpec, CondaSpec]
    ] = defaultdict(dict)

    logger.info("Checking for new versions")
    for s in sorted(available_specs, key=key_fn):
        key = s.version
        vv = key.version_str.info()
        
        reason = None
        if key in existing_versions:
            reason = "already exists"
        elif key.version_str.info() <= (4, 3, 30):
            reason = "too old"
        elif len(key.version_str.info()) >= 4:
            reason = "ignoring hotfix releases"
        
        if reason:
            logger.debug("Ignoring version %(s)s (%(reason)s)", locals())
            continue

        to_add[key][s] = s

    logger.info("Writing %s scripts", len(to_add))
    for ver, d in to_add.items():
        specs = list(d.values())
        fpath = out_dir / ver.to_filename()
        script_str = make_script(specs)
        logger.info("Writing script for %s", ver)
        if parsed.dry_run:
            print(f"Would write spec to {fpath}:\n" + textwrap.indent(script_str, "  "))
        else:
            with open(fpath, "w") as f:
                f.write(script_str)
