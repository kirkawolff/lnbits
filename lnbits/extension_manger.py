import hashlib
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from http import HTTPStatus
from typing import List, NamedTuple, Optional

import httpx
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

from lnbits.settings import settings


class Extension(NamedTuple):
    code: str
    is_valid: bool
    is_admin_only: bool
    name: Optional[str] = None
    short_description: Optional[str] = None
    tile: Optional[str] = None
    contributors: Optional[List[str]] = None
    hidden: bool = False
    migration_module: Optional[str] = None
    db_name: Optional[str] = None
    hash: Optional[str] = ""

    @property
    def module_name(self):
        return (
            f"lnbits.extensions.{self.code}"
            if self.hash == ""
            else f"lnbits.upgrades.{self.code}-{self.hash}.{self.code}"
        )

    @classmethod
    def from_installable_ext(cls, ext_info: "InstallableExtension") -> "Extension":
        return Extension(
            code=ext_info.id,
            is_valid=True,
            is_admin_only=False,  # todo: is admin only
            name=ext_info.name,
            hash=ext_info.hash if ext_info.module_installed else "",
        )


class ExtensionManager:
    def __init__(self, include_disabled_exts=False):
        self._disabled: List[str] = (
            [] if include_disabled_exts else settings.lnbits_disabled_extensions
        )
        self._admin_only: List[str] = settings.lnbits_admin_extensions
        self._extension_folders: List[str] = [
            x[1] for x in os.walk(os.path.join(settings.lnbits_path, "extensions"))
        ][0]

    @property
    def extensions(self) -> List[Extension]:
        output: List[Extension] = []

        if "all" in self._disabled:
            return output

        for extension in [
            ext for ext in self._extension_folders if ext not in self._disabled
        ]:
            try:
                with open(
                    os.path.join(
                        settings.lnbits_path, "extensions", extension, "config.json"
                    )
                ) as json_file:
                    config = json.load(json_file)
                is_valid = True
                is_admin_only = True if extension in self._admin_only else False
            except Exception:
                config = {}
                is_valid = False
                is_admin_only = False

            output.append(
                Extension(
                    extension,
                    is_valid,
                    is_admin_only,
                    config.get("name"),
                    config.get("short_description"),
                    config.get("tile"),
                    config.get("contributors"),
                    config.get("hidden") or False,
                    config.get("migration_module"),
                    config.get("db_name"),
                )
            )

        return output


class ExtensionRelease(BaseModel):
    name: str
    version: str
    archive: str
    source_repo: str
    hash: Optional[str]
    published_at: Optional[str]
    html_url: Optional[str]
    description: Optional[str]
    details_html: Optional[str] = None

    @classmethod
    def from_github_release(cls, source_repo: str, r: dict) -> "ExtensionRelease":
        return ExtensionRelease(
            name=r["name"],
            description=r["name"],
            version=r["tag_name"],
            archive=r["zipball_url"],
            source_repo=source_repo,
            # description=r["body"], # bad for JSON
            published_at=r["published_at"],
            html_url=r["html_url"],
        )

    @classmethod
    async def all_releases(cls, org, repo) -> List["ExtensionRelease"]:
        async with httpx.AsyncClient() as client:
            releases_url = f"https://api.github.com/repos/{org}/{repo}/releases"
            resp = await client.get(releases_url)
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=HTTPStatus.NOT_FOUND,
                    detail=f"Cannot fetch extension releases: {releases_url}: {resp.text}",
                )

            releases = resp.json()
            return [
                ExtensionRelease.from_github_release(f"{org}/{repo}", r)
                for r in releases
            ]


class InstallableExtension(BaseModel):
    id: str
    name: str
    short_description: Optional[str] = None
    icon: Optional[str] = None
    icon_url: Optional[str] = None
    dependencies: List[str] = []
    is_admin_only: bool = False
    stars: int = 0
    latest_release: Optional[ExtensionRelease]
    installed_release: Optional[ExtensionRelease]

    @property
    def hash(self) -> str:
        if self.installed_release:
            if self.installed_release.hash:
                return self.installed_release.hash
            m = hashlib.sha256()
            m.update(f"{self.installed_release.archive}".encode())
            return m.hexdigest()
        return "not-installed"

    @property
    def zip_path(self) -> str:
        extensions_data_dir = os.path.join(settings.lnbits_data_folder, "extensions")
        os.makedirs(extensions_data_dir, exist_ok=True)
        return os.path.join(extensions_data_dir, f"{self.id}.zip")

    @property
    def ext_dir(self) -> str:
        return os.path.join("lnbits", "extensions", self.id)

    @property
    def ext_upgrade_dir(self) -> str:
        return os.path.join("lnbits", "upgrades", f"{self.id}-{self.hash}")

    @property
    def module_name(self) -> str:
        return f"lnbits.extensions.{self.id}"

    @property
    def module_installed(self) -> bool:
        return self.module_name in sys.modules

    def download_archive(self):
        ext_zip_file = self.zip_path
        if os.path.isfile(ext_zip_file):
            os.remove(ext_zip_file)
        try:
            download_url(self.installed_release.archive, ext_zip_file)
        except Exception as ex:
            logger.warning(ex)
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail="Cannot fetch extension archive file",
            )

        archive_hash = file_hash(ext_zip_file)
        if self.installed_release.hash and self.installed_release.hash != archive_hash:
            # remove downloaded archive
            if os.path.isfile(ext_zip_file):
                os.remove(ext_zip_file)
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail="File hash missmatch. Will not install.",
            )

    def extract_archive(self):
        os.makedirs(os.path.join("lnbits", "upgrades"), exist_ok=True)
        shutil.rmtree(self.ext_upgrade_dir, True)
        with zipfile.ZipFile(self.zip_path, "r") as zip_ref:
            zip_ref.extractall(self.ext_upgrade_dir)
        generated_dir_name = os.listdir(self.ext_upgrade_dir)[0]
        os.rename(
            os.path.join(self.ext_upgrade_dir, generated_dir_name),
            os.path.join(self.ext_upgrade_dir, self.id),
        )

        shutil.rmtree(self.ext_dir, True)
        shutil.copytree(
            os.path.join(self.ext_upgrade_dir, self.id),
            os.path.join("lnbits", "extensions", self.id),
        )

    def nofiy_upgrade(self) -> None:
        """Update the the list of upgraded extensions. The middleware will perform redirects based on this"""
        if not self.hash:
            return

        clean_upgraded_exts = list(
            filter(
                lambda old_ext: not old_ext.endswith(f"/{self.id}"),
                settings.lnbits_upgraded_extensions,
            )
        )
        settings.lnbits_upgraded_extensions = clean_upgraded_exts + [
            f"{self.hash}/{self.id}"
        ]

    def clean_extension_files(self):
        # remove downloaded archive
        if os.path.isfile(self.zip_path):
            os.remove(self.zip_path)

        # remove module from extensions
        shutil.rmtree(self.ext_dir, True)

        shutil.rmtree(self.ext_upgrade_dir, True)

    @classmethod
    def from_row(cls, data: dict) -> "InstallableExtension":
        meta = json.loads(data["meta"])
        ext = InstallableExtension(**data)
        if "installed_release" in meta:
            ext.installed_release = ExtensionRelease(**meta["installed_release"])
        return ext

    @classmethod
    async def from_repo(
        cls, ext_id, org, repository
    ) -> Optional["InstallableExtension"]:
        try:
            repo, latest_release, config = await fetch_github_repo_info(org, repository)

            return InstallableExtension(
                id=ext_id,
                name=config.get("name"),
                short_description=config.get("short_description"),
                version="0",
                stars=repo["stargazers_count"],
                icon_url=icon_to_github_url(org, config.get("tile")),
                latest_release=ExtensionRelease.from_github_release(
                    repo["html_url"], latest_release
                ),
            )
        except Exception as e:
            logger.warning(e)
        return None

    @classmethod
    def from_manifest(cls, e: dict) -> "InstallableExtension":
        return InstallableExtension(
            id=e["id"],
            name=e["name"],
            archive=e["archive"],
            hash=e["hash"],
            short_description=e["shortDescription"],
            icon=e["icon"],
            dependencies=e["dependencies"] if "dependencies" in e else [],
        )

    @classmethod
    async def get_installable_extensions(
        cls, installed_extensions: List["InstallableExtension"] = []
    ) -> List["InstallableExtension"]:
        extension_list: List[InstallableExtension] = (
            installed_extensions if installed_extensions else []
        )
        extension_id_list: List[str] = [e.id for e in extension_list]

        async with httpx.AsyncClient() as client:
            for url in settings.lnbits_extensions_manifests:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        logger.warning(f"Cannot fetch extensions manifest at: {url}")
                        continue
                    manifest = resp.json()
                    if "repos" in manifest:
                        for r in manifest["repos"]:
                            if r["id"] in extension_id_list:
                                continue
                            ext = await InstallableExtension.from_repo(
                                r["id"], r["organisation"], r["repository"]
                            )
                            if ext:
                                extension_list += [ext]
                                extension_id_list += [ext.id]

                    if "extensions" in manifest:
                        for e in manifest["extensions"]:
                            if e["id"] in extension_id_list:
                                continue
                            extension_list += [InstallableExtension.from_manifest(e)]
                            extension_id_list += [e["id"]]
                except Exception as e:
                    logger.warning(f"Manifest {url} failed with '{str(e)}'")

        return extension_list

    @classmethod
    async def get_extension_releases(cls, ext_id: str) -> List["ExtensionRelease"]:
        extension_releases: List[ExtensionRelease] = []
        async with httpx.AsyncClient() as client:
            for url in settings.lnbits_extensions_manifests:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        logger.warning(f"Cannot fetch extensions manifest at: {url}")
                        continue
                    manifest = resp.json()
                    if "repos" in manifest:
                        for r in manifest["repos"]:
                            if r["id"] == ext_id:
                                repo_releases = await ExtensionRelease.all_releases(
                                    r["organisation"], r["repository"]
                                )
                                extension_releases += repo_releases

                    if "extensions" in manifest:
                        for e in manifest["extensions"]:
                            if e["id"] == ext_id:
                                extension_releases += [
                                    ExtensionRelease(
                                        name=e["name"],
                                        version=e["version"],
                                        archive=e["archive"],
                                        hash=e["hash"],
                                        source_repo=url,
                                        description=e["shortDescription"],
                                        details_html=e.get("details"),
                                    )
                                ]

                except Exception as e:
                    logger.warning(f"Manifest {url} failed with '{str(e)}'")

        return extension_releases

    @classmethod
    async def get_extension_release(
        cls, ext_id: str, source_repo: str, archive: str
    ) -> Optional["ExtensionRelease"]:
        all_releases: List[
            ExtensionRelease
        ] = await InstallableExtension.get_extension_releases(ext_id)
        selected_release = [
            r
            for r in all_releases
            if r.archive == archive and r.source_repo == source_repo
        ]

        return selected_release[0] if len(selected_release) != 0 else None


class InstalledExtensionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not "path" in scope:
            await self.app(scope, receive, send)
            return

        path_elements = scope["path"].split("/")
        if len(path_elements) > 2:
            _, path_name, path_type, *rest = path_elements
        else:
            _, path_name = path_elements
            path_type = None

        # block path for all users if the extension is disabled
        if path_name in settings.lnbits_disabled_extensions:
            response = JSONResponse(
                status_code=HTTPStatus.NOT_FOUND,
                content={"detail": f"Extension '{path_name}' disabled"},
            )
            await response(scope, receive, send)
            return

        # re-route API trafic if the extension has been upgraded
        if path_type == "api":
            upgraded_extensions = list(
                filter(
                    lambda ext: ext.endswith(f"/{path_name}"),
                    settings.lnbits_upgraded_extensions,
                )
            )
            if len(upgraded_extensions) != 0:
                upgrade_path = upgraded_extensions[0]
                tail = "/".join(rest)
                scope["path"] = f"/upgrades/{upgrade_path}/{path_type}/{tail}"

        await self.app(scope, receive, send)


class CreateExtension(BaseModel):
    ext_id: str
    archive: str
    source_repo: str


def get_valid_extensions(include_disabled_exts=False) -> List[Extension]:
    return [
        extension
        for extension in ExtensionManager(include_disabled_exts).extensions
        if extension.is_valid
    ]


def download_url(url, save_path):
    with urllib.request.urlopen(url) as dl_file:
        with open(save_path, "wb") as out_file:
            out_file.write(dl_file.read())


def file_hash(filename):
    h = hashlib.sha256()
    b = bytearray(128 * 1024)
    mv = memoryview(b)
    with open(filename, "rb", buffering=0) as f:
        while n := f.readinto(mv):
            h.update(mv[:n])
    return h.hexdigest()


def icon_to_github_url(org: str, path: Optional[str]) -> str:
    if not path:
        return ""
    _, repo, *rest = path.split("/")
    tail = "/".join(rest)
    return f"https://github.com/{org}/{repo}/raw/main/{tail}"


async def fetch_github_repo_info(org: str, repository: str):
    async with httpx.AsyncClient() as client:
        repo_url = f"https://api.github.com/repos/{org}/{repository}"
        resp = await client.get(repo_url)
        if resp.status_code != 200:
            detail = f"Cannot fetch extension repo: {repo_url}: {resp.text}"
            logger.warning(detail)
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=detail,
            )
        repo = resp.json()

        lates_release_url = (
            f"https://api.github.com/repos/{org}/{repository}/releases/latest"
        )
        resp = await client.get(lates_release_url)
        if resp.status_code != 200:
            detail = (
                f"Cannot fetch extension releases: {lates_release_url}: {resp.text}"
            )
            logger.warning(detail)
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=detail,
            )

        latest_release = resp.json()

        config_url = f"""https://raw.githubusercontent.com/{org}/{repository}/{repo["default_branch"]}/config.json"""
        resp = await client.get(config_url)
        if resp.status_code != 200:
            detail = f"Cannot fetch config for extension: {config_url}: {resp.text}"
            logger.warning(detail)
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=detail,
            )

        config = resp.json()

        return repo, latest_release, config
