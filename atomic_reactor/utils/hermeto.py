# Copyright (c) 2024 Red Hat, Inc
# All rights reserved.
#
# This software may be modified and distributed under the terms
# of the BSD license. See the LICENSE file for details.

"""
Utils to help to integrate with Hermeto CLI tool
"""

import logging

from typing import Any, Callable, Dict, Optional, Tuple, List
from pathlib import Path
import os.path
import urllib

import git
from packageurl import PackageURL

from atomic_reactor import constants
from atomic_reactor.utils import retries

logger = logging.getLogger(__name__)


class SymlinkSandboxError(Exception):
    """Found symlink(s) pointing outside the sandbox."""


def enforce_sandbox(repo_root: Path, remove_unsafe_symlinks: bool = False) -> None:
    """
    Check that there are no symlinks that try to leave the cloned repository.

    :param (str | Path) repo_root: absolute path to root of cloned repository
    :param bool remove_unsafe_symlinks: remove unsafe symlinks if any are found
    :raises OsbsValidationException: if any symlink points outside of cloned repository
    """
    for path_to_dir, subdirs, files in os.walk(repo_root):
        dirpath = Path(path_to_dir)

        for entry in subdirs + files:
            # the logic in here actually *requires* f-strings with `!r`. using
            # `%r` DOES NOT WORK (tested)
            # pylint: disable=logging-fstring-interpolation

            # apparently pylint doesn't understand Path
            full_path = dirpath / entry  # pylint: disable=old-division

            try:
                real_path = full_path.resolve()
            except RuntimeError as e:
                if "Symlink loop from " in str(e):
                    logger.info(f"Symlink loop from {full_path!r}")
                    continue
                logger.exception("RuntimeError encountered")
                raise

            try:
                real_path.relative_to(repo_root)
            except ValueError as exc:
                # Unlike the real path, the full path is always relative to the root
                relative_path = str(full_path.relative_to(repo_root))
                if remove_unsafe_symlinks:
                    full_path.unlink()
                    logger.warning(
                        f"The destination of {relative_path!r} is outside of cloned repository. "
                        "Removing...",
                    )
                else:
                    raise SymlinkSandboxError(
                        f"The destination of {relative_path!r} is outside of cloned repository",
                    ) from exc


def validate_paths(repo_path: Path, remote_sources_packages: dict) -> None:
    """Paths must be relative and within cloned repo"""
    def is_path_ok(path_string):
        path = Path(path_string)
        if path.is_absolute():
            return False

        # using real repo to properly resolve and block bad symlinks
        full_path = (repo_path/path).resolve()

        # using commonpath to be compatible with py3.8
        if os.path.commonpath((full_path, repo_path)) != str(repo_path):
            return False

        return True

    for pkg_mgr, options in remote_sources_packages.items():
        if not options:
            continue

        for option in options:
            for key, val in option.items():
                if key == "path":
                    if not is_path_ok(val):
                        raise ValueError(
                            f"{pkg_mgr}:{key}: path '{val}' must be relative "
                            "within remote source repository"
                        )
                elif (
                    pkg_mgr == "pip" and
                    key in ("requirements_files", "requirements_build_files")
                ):
                    for v in val:
                        if not is_path_ok(v):
                            raise ValueError(
                                f"{pkg_mgr}:{key}: path '{v}' must be relative "
                                "within remote source repository"
                            )
                else:
                    raise ValueError(f"unexpected key '{key}' in '{pkg_mgr}' config")


def normalize_gomod_pkg_manager(remote_source: Dict[str, Any]):
    """Cachito compatibility, empty/undefined pkg_managers means gomod.
    Replace it to explicitly use gomod

    Function does in-place change.
    """
    pkg_managers = remote_source.get("pkg_managers")
    if pkg_managers is None:
        # Cachito behavior, missing pkg_managers means to use gomod
        pkg_managers = ["gomod"]
    remote_source["pkg_managers"] = pkg_managers


def remote_source_to_hermeto(remote_source: Dict[str, Any]) -> Dict[str, Any]:
    """Converts remote source into Hermeto expected params.

    Remote sources were orignally designed for cachito. Hermeto is not a direct
    fork but has lot of similarities.
    However, some parameters must be updated to be compatible with Hermeto.

    Removed flags (OSBS process them):
    * include-git-dir
    * remove-unsafe-symlinks

    Removed pkg-managers (OSBS process them):
    * git-submodule

    """
    pkg_managers_map = {
        "rubygems": "bundler"  # renamed in Hermeto
    }

    removed_flags = {"include-git-dir", "remove-unsafe-symlinks"}
    removed_pkg_managers = {"git-submodule"}

    hermeto_flags = sorted(
        set(remote_source.get("flags") or []) - removed_flags
    )
    hermeto_packages = []

    normalize_gomod_pkg_manager(remote_source)

    pkg_managers = remote_source["pkg_managers"]

    for pkg_manager in pkg_managers:
        if pkg_manager in removed_pkg_managers:
            continue

        packages = remote_source.get("packages", {}).get(pkg_manager, [])
        packages = packages or [{"path": "."}]

        # if pkg manager has different name in Hermeto update it
        pkg_manager = pkg_managers_map.get(pkg_manager, pkg_manager)

        for pkg in packages:
            hermeto_packages.append({"type": pkg_manager, **pkg})

    return {"packages": hermeto_packages, "flags": hermeto_flags}


def convert_SBOM_to_ICM(sbom: Dict[str, Any]) -> Dict[str, Any]:
    """Function converts Hermeto SBOM into ICM

    Unfortunately Hermeto doesn't provide all details about dependencies
    and sources, so the ICM can contain only flat structure of everything
    """
    icm = {
        "metadata": {
            "icm_spec": (
                "https://raw.githubusercontent.com/containerbuildsystem/atomic-reactor/"
                "f4abcfdaf8247a6b074f94fa84f3846f82d781c6/atomic_reactor/schemas/"
                "content_manifest.json"
            ),
            "icm_version": 1,
            "image_layer_index": -1
        },
        "image_contents": [],
    }
    icm["image_contents"] = [
        {"purl": comp["purl"]} for comp in sbom["components"]  # type: ignore
    ]
    return icm


def gen_dependency_from_sbom_component(sbom_dep: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Generate a single request.json dependency from a SBOM component

    Dependency type is derived from purl.
    Version is decided on how Cachito would do it.
    """
    # we need to detect type from purl, this is just heuristics,
    # we cannot reliably construct type from purl
    purl = PackageURL.from_string(sbom_dep["purl"])
    heuristic_type = purl.type or "unknown"  # for unknown types, reuse what's in purl type
    # types supported by cachito/Hermeto
    purl_type_matchers: Tuple[Tuple[Callable[[PackageURL], bool], str], ...] = (
        (lambda p: p.type == "golang" and p.qualifiers.get("type", "") == "module", "gomod"),
        (lambda p: p.type == "golang", "go-package"),
        (lambda p: p.type == "npm", "npm"),
        (lambda p: p.type == "pypi", "pip"),
        (lambda p: p.type == "rpm", "rpm"),
        (lambda p: p.type == "gem", "rubygems"),
        (lambda p: p.type == "cargo", "cargo"),
    )

    for matcher, request_type in purl_type_matchers:
        if matcher(purl):
            heuristic_type = request_type
            break

    pkg_dot_path = ("golang", "gem")

    version = (
        # for non-registry dependencies cachito uses URL as version
        purl.qualifiers.get("vcs_url") or
        purl.qualifiers.get("download_url") or
        # for local dependencies Cachito uses path as version
        (f"./{purl.subpath}" if purl.subpath and purl.type in pkg_dot_path else None) or
        (f"file:{purl.subpath}" if purl.subpath and purl.type not in pkg_dot_path else None) or
        # version is mainly for dependencies from pkg registries
        sbom_dep.get("version")
        # returns None if version cannot be determined
    )

    if version and purl.subpath and not version.endswith(purl.subpath):
        # include subpath into vcs or download url to get exact location of dependency
        # used mainly for vendored deps
        version = f"{version}#{purl.subpath}"

    res = {
        "name": sbom_dep["name"],
        "replaces": None,  # it's always None, replacements aren't supported by Hermeto
        "type": heuristic_type,
        "version": version,
    }

    # dev package definition
    # currently only NPM and Pip
    if any(p["name"] == "cdx:npm:package:development" and p["value"] == "true" or
           p["name"] == "cdx:pip:package:build-dependency" and p["value"] == "true"
           for p in sbom_dep.get("properties", [])):
        res["dev"] = True

    return res


def generate_request_json(
    remote_source: Dict[str, Any], remote_source_sbom: Dict[str, Any],
    remote_source_env_json: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Generates Cachito like request.json

    Cachito does provide request.json, for backward compatibility
    as some tools are depending on it, we have to generate also request.json from Hermeto
    """

    res = {
        "dependencies": [
            gen_dependency_from_sbom_component(dep)
            for dep in remote_source_sbom["components"]
        ],
        "pkg_managers": remote_source.get("pkg_managers", []),
        "ref": remote_source["ref"],
        "repo": remote_source["repo"],
        "environment_variables": {env['name']: env["value"] for env in remote_source_env_json},
        "flags": remote_source.get("flags") or [],
        "packages": [],  # this will be always empty Hermeto doesn't provide nested deps
    }
    return res


def clone_only(remote_source: Dict[str, Any]) -> bool:
    """Determine if only cloning is required without Hermeto run"""

    pkg_managers = remote_source.get("pkg_managers")

    if pkg_managers is not None and len(pkg_managers) == 0:
        return True

    # only git-submodule
    if pkg_managers is not None and pkg_managers == ['git-submodule']:
        return True

    return False


def has_git_submodule_manager(remote_source: Dict[str, Any]) -> bool:
    """Returns true when for specific remote source git-submodule manager is requested"""
    pkg_managers = remote_source.get("pkg_managers") or []
    return 'git-submodule' in pkg_managers


def update_submodules(repopath: Path):
    """Update submodules in the given repo"""
    cmd = ["git", "submodule", "update", "--init", "--filter=blob:none"]
    params = {
        "cwd": str(repopath),
        "timeout": constants.GIT_CMD_TIMEOUT,
    }
    retries.run_cmd(cmd, **params)


def get_submodules_sbom_components(repo: git.Repo) -> List[Dict]:
    """Get SBOM components of submodules in the specified repository"""

    def to_vcs_purl(pkg_name, repo_url, ref):
        """
        Generate the vcs purl representation of the package.

        Use the most specific purl type possible, e.g. pkg:github if repo comes from
        github.com. Fall back to using pkg:generic with a ?vcs_url qualifier.

        :param str pkg_name: name of package
        :param str repo_url: url of git repository for package
        :param str ref: git ref of package
        :return: the PURL string of the Package object
        :rtype: str
        """
        repo_url = repo_url.rstrip("/")
        parsed_url = urllib.parse.urlparse(repo_url)

        pkg_type_for_hostname = {
            "github.com": "github",
            "bitbucket.org": "bitbucket",
        }
        pkg_type = pkg_type_for_hostname.get(parsed_url.hostname, "generic")

        if pkg_type == "generic":
            vcs_url = urllib.parse.quote(f"{repo_url}@{ref}", safe="")
            purl = f"pkg:generic/{pkg_name}?vcs_url={vcs_url}"
        else:
            # pkg:github and pkg:bitbucket use the same format
            namespace, repo = parsed_url.path.lstrip("/").rsplit("/", 1)
            if repo.endswith(".git"):
                repo = repo[: -len(".git")]
            purl = f"pkg:{pkg_type}/{namespace.lower()}/{repo.lower()}@{ref}"

        return purl

    submodules_sbom_components = [
        {
            "type": "library",
            "name": sm.name,
            "version": f"{sm.url}#{sm.hexsha}",
            "purl": to_vcs_purl(sm.name, sm.url, sm.hexsha)
        }
        for sm in repo.submodules
    ]

    return submodules_sbom_components


def get_submodules_request_json_deps(repo: git.Repo) -> List[Dict]:
    """Get dependencies for request.json from submodule"""
    submodules_request_json_dependencies = [
        {
            "type": "git-submodule",
            "name": sm.name,
            "path": sm.name,
            "version": f"{sm.url}#{sm.hexsha}",
        }
        for sm in repo.submodules
    ]

    return submodules_request_json_dependencies
