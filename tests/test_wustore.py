"""Minecraft Education from Windows Update (#351): bol.wustore."""
# SPDX-License-Identifier: MIT

import base64
import hashlib
import html
import io
import json
import ssl
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from bol import wustore
from bol.config import MC_PRODUCTS

EDUCATION = next(e for e in MC_PRODUCTS if e["id"] == "education")
FAMILY = "Microsoft.MinecraftEducationEdition_8wekyb3d8bbwe"
BUNDLE_MONIKER = ("Microsoft.MinecraftEducationEdition_2026.1021.2142.0_"
                  "neutral_~_8wekyb3d8bbwe")
APPX_NAME = "Microsoft.MinecraftEducationEdition_1.26.3300.0_x64_8wekyb3d8bbwe.appx"

BUNDLE_MANIFEST = f"""<?xml version="1.0" encoding="UTF-8"?>
<Bundle xmlns="http://schemas.microsoft.com/appx/2013/bundle">
  <Identity Name="Microsoft.MinecraftEducationEdition" Version="2026.1021.2142.0"/>
  <Packages>
    <Package Type="resource" Version="1.26.3300.0" ResourceId="split.scale-200"
             FileName="Resources.appx" Offset="0" Size="1"/>
    <Package Type="application" Version="1.26.3200.0" Architecture="x86"
             FileName="x86.appx" Offset="0" Size="1"/>
    <Package Type="application" Version="1.26.3300.0" Architecture="x64"
             FileName="{APPX_NAME}" Offset="100" Size="1"/>
  </Packages>
</Bundle>"""


def _sync_answer(packages, identities):
    """A SyncUpdates answer the way FE3 writes it: XML escaped in XML."""
    new = "".join(
        f"<UpdateInfo><ID>{number}</ID><Deployment/><IsLeaf>true</IsLeaf>"
        f"<Xml>{html.escape(xml)}</Xml></UpdateInfo>"
        for number, xml in identities)
    extended = "".join(
        f"<Update><ID>{number}</ID><Xml>{html.escape(xml)}</Xml></Update>"
        for number, xml in packages)
    return (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        '<s:Body><SyncUpdatesResponse xmlns="http://www.microsoft.com/'
        'SoftwareDistribution/Server/ClientWebService"><SyncUpdatesResult>'
        f"<NewUpdates>{new}</NewUpdates>"
        f"<ExtendedUpdateInfo><Updates>{extended}</Updates>"
        "</ExtendedUpdateInfo></SyncUpdatesResult></SyncUpdatesResponse>"
        "</s:Body></s:Envelope>").encode()


def _identity(update_id, revision="1"):
    return (f'<UpdateIdentity UpdateID="{update_id}" '
            f'RevisionNumber="{revision}" /><Properties />')


def _files(name, moniker, size=10, sha1="c2hhMQ==", sha256="c2hhMjU2"):
    return (
        '<ExtendedProperties DefaultPropertiesLanguage="en" />'
        f'<Files><File FileName="{name}" Digest="{sha1}" DigestAlgorithm="SHA1"'
        f' Size="{size}" InstallerSpecificIdentifier="{moniker}">'
        f'<AdditionalDigest Algorithm="SHA256">{sha256}</AdditionalDigest>'
        '</File><File FileName="Abm_x.cab" Digest="eA==" Size="3" '
        'PatchingType="DynamicMetadata" /></Files>')


def _location_answer(pairs):
    locations = "".join(
        f"<FileLocation><FileDigest>{digest}</FileDigest><Url>{url}</Url>"
        "</FileLocation>" for digest, url in pairs)
    return (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        "<s:Body><GetExtendedUpdateInfo2Response xmlns=\"http://www.microsoft"
        ".com/SoftwareDistribution/Server/ClientWebService\">"
        f"<GetExtendedUpdateInfo2Result><FileLocations>{locations}"
        "</FileLocations></GetExtendedUpdateInfo2Result>"
        "</GetExtendedUpdateInfo2Response></s:Body></s:Envelope>").encode()


def _cookie_answer():
    return (b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            b"<s:Body><GetCookieResponse><GetCookieResult><EncryptedData>"
            b"Y29va2ll</EncryptedData></GetCookieResult></GetCookieResponse>"
            b"</s:Body></s:Envelope>")


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _appx(files):
    """An application package: a zip whose names are percent-encoded."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return out.getvalue()


def _bundle(appx_bytes, manifest=BUNDLE_MANIFEST, stored=True):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo(APPX_NAME),
            appx_bytes,
            compress_type=zipfile.ZIP_STORED if stored
            else zipfile.ZIP_DEFLATED)
        archive.writestr("AppxMetadata/AppxBundleManifest.xml", manifest,
                         compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("AppxSignature.p7x", b"sig")
    return out.getvalue()


GAME_FILES = {
    "Minecraft.Windows.exe": b"MZ plaintext",
    "AppxManifest.xml": (
        '<Package><Identity Name="Microsoft.MinecraftEducationEdition" '
        'Version="1.26.3300.0" /></Package>'),
    "Assets/Square44x44Logo%20dark.png": b"png",
    "data/resource_packs/vanilla/manifest.json": b"{}",
    "AppxBlockMap.xml": b"<BlockMap/>",
    "AppxSignature.p7x": b"sig",
    "[Content_Types].xml": b"<Types/>",
}


class TrustTests(unittest.TestCase):
    def test_fe3_is_verified_against_microsofts_2011_root_only(self):
        context = wustore._ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        roots = context.get_ca_certs()
        self.assertEqual(len(roots), 1)
        subject = dict(item[0] for item in roots[0]["subject"])
        self.assertEqual(subject["commonName"],
                         "Microsoft Root Certificate Authority 2011")
        der = ssl.PEM_cert_to_DER_cert(wustore.MICROSOFT_ROOT_2011)
        self.assertEqual(hashlib.sha1(der).hexdigest().upper(),
                         "8F43288AD272F3103B6FB1428485EA3014C0BCFE")


class CatalogueTests(unittest.TestCase):
    def test_the_education_edition_is_a_windows_update_one(self):
        self.assertEqual(EDUCATION["source"], "windows-update")
        self.assertEqual(EDUCATION["family"], FAMILY)
        self.assertFalse(EDUCATION["xbox_live"])
        self.assertFalse(EDUCATION["beta"])

    def test_a_sync_answer_joins_identities_to_files(self):
        answer = _sync_answer(
            packages=[
                ("10", _files("bundle.appxbundle", BUNDLE_MONIKER, 1266)),
                ("11", _files("vclibs.appx",
                              "Microsoft.VCLibs.140.00.UWPDesktop_14.0.1_x64"
                              "__8wekyb3d8bbwe")),
                ("12", "<Files><File FileName='orphan.appx' "
                       "InstallerSpecificIdentifier='A_1_x64__p' /></Files>"),
            ],
            identities=[("10", _identity("bundle-id", "3")),
                        ("11", _identity("vclibs-id"))])
        packages = wustore.parse_sync(wustore.ET.fromstring(answer))

        self.assertEqual([p["update_id"] for p in packages],
                         ["bundle-id", "vclibs-id"])
        bundle = packages[0]
        self.assertEqual(bundle["revision"], "3")
        self.assertEqual(bundle["moniker"], BUNDLE_MONIKER)
        self.assertEqual(bundle["size"], 1266)
        self.assertEqual(bundle["sha256"], "c2hhMjU2")
        # The metadata cab beside the bundle is not a package.
        self.assertNotIn("Abm_x.cab", [p["file_name"] for p in packages])

    def test_the_newest_package_of_the_family_wins_a_bundle_first(self):
        packages = [
            {"moniker": "Other.App_9.0.0.0_neutral_~_8wekyb3d8bbwe",
             "file_name": "o.appxbundle"},
            {"moniker": f"{FAMILY.split('_')[0]}_2026.1.0.0_neutral_~_"
                        "8wekyb3d8bbwe", "file_name": "old.appxbundle"},
            {"moniker": BUNDLE_MONIKER, "file_name": "new.appxbundle"},
            {"moniker": f"{FAMILY.split('_')[0]}_2026.1021.2142.0_arm64__"
                        "8wekyb3d8bbwe", "file_name": "arm.appx"},
        ]
        best = wustore.newest_package(packages, FAMILY)
        self.assertEqual(best["file_name"], "new.appxbundle")
        self.assertEqual(best["package_version"], "2026.1021.2142.0")
        self.assertIsNone(wustore.newest_package(packages, "Nope_x"))

    def test_package_versions_read_as_minecraft_versions(self):
        self.assertEqual(wustore.game_version("1.26.3300.0"), "1.26.33.0")
        self.assertEqual(wustore.game_version("1.21.12004.0"), "1.21.120.4")
        self.assertEqual(wustore.game_version("1.0.12.0"), "1.0.12")
        self.assertEqual(wustore.game_version("odd"), "odd")

    def test_the_bundle_manifest_names_the_x64_game(self):
        name, version = wustore.bundle_application(BUNDLE_MANIFEST.encode())
        self.assertEqual((name, version), (APPX_NAME, "1.26.3300.0"))
        with self.assertRaises(wustore.WindowsUpdateError):
            wustore.bundle_application(
                BUNDLE_MANIFEST.replace('"x64"', '"arm64"').encode())

    def test_the_game_version_is_read_from_a_remote_bundle_in_place(self):
        bundle = _bundle(_appx(GAME_FILES))
        asked = []

        def fetch(start, end):
            asked.append((start, end))
            return bundle[start:end + 1]

        version = wustore.remote_game_version(
            "http://tlu.example/file", len(bundle), fetch)
        self.assertEqual(version, "1.26.33.0")
        self.assertTrue(asked)
        self.assertLessEqual(len(asked), 3)

    def test_a_short_range_is_an_error_not_a_misread(self):
        reader = wustore._RangeReader("http://x", 100, lambda s, e: b"abc")
        with self.assertRaises(wustore.WindowsUpdateError):
            reader.read(10)

    def _opener(self, sync, locations):
        """An FE3 stand-in that answers each SOAP action in turn."""
        calls = []

        def opener(request):
            action = request.data.decode()
            calls.append(request.full_url)
            if "GetCookie" in action and "SyncUpdates" not in action:
                return _Answer(_cookie_answer())
            if "<SyncUpdates" in action:
                return _Answer(sync)
            return _Answer(locations)

        opener.calls = calls
        return opener

    def test_the_catalogue_names_the_current_build_by_its_game_version(self):
        bundle = _bundle(_appx(GAME_FILES))
        sync = _sync_answer(
            [("1", _files("b.appxbundle", BUNDLE_MONIKER, len(bundle),
                          sha1="QUJD"))],
            [("1", _identity("uid", "2"))])
        opener = self._opener(sync, _location_answer(
            [("QUJD", "http://tlu.dl.delivery.mp.microsoft.com/f?P1=1")]))
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(wustore, "CACHE", Path(td)):
            catalogue = wustore.version_catalogue(
                EDUCATION, opener=opener,
                fetch=lambda s, e: bundle[s:e + 1])
            cached = json.loads(
                (Path(td) / "wu-education.json").read_text())

        self.assertEqual([entry["version"] for entry in catalogue],
                         ["1.26.33.0"])
        self.assertEqual(catalogue[0]["urls"], [])
        self.assertEqual(catalogue[0]["package"]["update_id"], "uid")
        self.assertEqual(cached, catalogue)
        self.assertEqual(opener.calls,
                         [wustore.FE3, wustore.FE3, wustore.FE3_SECURED])

    def test_an_unreachable_windows_update_falls_back_to_the_last_list(self):
        listed = [{"version": "1.26.33.0", "urls": [], "package": {}}]

        def down(_request):
            raise OSError("network is unreachable")

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(wustore, "CACHE", Path(td)):
            with self.assertRaises(wustore.WindowsUpdateError):
                wustore.version_catalogue(EDUCATION, opener=down)
            (Path(td) / "wu-education.json").write_text(json.dumps(listed))
            # Fresh: served without asking at all.
            self.assertEqual(
                wustore.version_catalogue(EDUCATION, opener=down), listed)
            # Asked past the cache: the stale copy still beats nothing.
            self.assertEqual(
                wustore.version_catalogue(EDUCATION, ignore_cache=True,
                                          opener=down), listed)

    def test_no_package_of_the_family_is_said_so(self):
        sync = _sync_answer([], [])
        with self.assertRaisesRegex(wustore.WindowsUpdateError,
                                    "lists no Minecraft Education package"):
            wustore.latest_build(EDUCATION,
                                 opener=self._opener(sync, b""))


class UnpackTests(unittest.TestCase):
    def test_member_paths_are_decoded_and_kept_inside(self):
        safe = wustore._safe_member_path
        self.assertEqual(str(safe("Assets/My%20Logo.png")),
                         "Assets/My Logo.png")
        self.assertEqual(str(safe("data\\x.json")), "data/x.json")
        for bad in ("../evil", "a/../../evil", "/etc/passwd", "%2e%2e/evil",
                    "C:/evil", "dir/"):
            with self.subTest(name=bad):
                self.assertIsNone(safe(bad))
        for metadata in ("AppxBlockMap.xml", "AppxSignature.p7x",
                         "[Content_Types].xml"):
            self.assertIsNone(safe(metadata))
        # Only the package's own metadata at its root is dropped.
        self.assertIsNotNone(safe("data/AppxBlockMap.xml"))

    def test_a_bundle_unpacks_to_a_game_the_launcher_recognises(self):
        from bol import games, xodus

        with tempfile.TemporaryDirectory() as td:
            bundle = Path(td) / "edu.appxbundle"
            bundle.write_bytes(_bundle(_appx(GAME_FILES)))
            dest = Path(td) / "games" / "education" / "1.26.33.0"
            dest.mkdir(parents=True)
            seen = []
            wustore.unpack_package(bundle, dest,
                                   lambda done, total: seen.append(
                                       (done, total)))

            self.assertEqual((dest / "Minecraft.Windows.exe").read_bytes(),
                             b"MZ plaintext")
            self.assertTrue((dest / "Assets" / "Square44x44Logo dark.png")
                            .is_file())
            for metadata in ("AppxBlockMap.xml", "AppxSignature.p7x",
                             "[Content_Types].xml"):
                self.assertFalse((dest / metadata).exists())
            self.assertEqual(xodus.game_root(dest), dest)
            self.assertFalse(xodus.lost_package_cache(dest))
            self.assertEqual(games.mc_version_str(dest), "1.26.33.0")
            self.assertEqual(seen[-1][0], seen[-1][1])

    def test_a_compressed_game_package_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = Path(td) / "edu.appxbundle"
            bundle.write_bytes(_bundle(_appx(GAME_FILES), stored=False))
            with self.assertRaisesRegex(wustore.WindowsUpdateError,
                                        "compressed"):
                wustore.unpack_package(bundle, Path(td) / "out")

    def test_unpacking_needs_room_for_the_game(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = Path(td) / "edu.appxbundle"
            bundle.write_bytes(_bundle(_appx(GAME_FILES)))
            dest = Path(td) / "out"
            dest.mkdir()
            with mock.patch.object(wustore, "_free_space", return_value=10), \
                    self.assertRaisesRegex(wustore.WindowsUpdateError,
                                           "Not enough free space"):
                wustore.unpack_package(bundle, dest)
            self.assertEqual(list(dest.iterdir()), [])


class InstallTests(unittest.TestCase):
    def _entry(self, bundle_bytes, sha256=None):
        digest = sha256 or base64.b64encode(
            hashlib.sha256(bundle_bytes).digest()).decode()
        return {"version": "1.26.33.0", "urls": [], "package": {
            "update_id": "uid", "revision": "1", "sha1": "QUJD",
            "sha256": digest, "size": len(bundle_bytes),
            "file_name": "b.appxbundle", "moniker": BUNDLE_MONIKER}}

    def _install(self, root, bundle_bytes, entry):
        def fake_download(url, dest, label=None, progress=None):
            self.assertEqual(url, "http://tlu/f")
            dest.write_bytes(bundle_bytes)
            return dest

        with mock.patch.object(wustore, "file_urls",
                               return_value={"QUJD": "http://tlu/f"}), \
                mock.patch.object(wustore, "download",
                                  side_effect=fake_download), \
                mock.patch.object(wustore, "info"), \
                mock.patch.object(wustore, "ok"):
            return wustore.install(EDUCATION, entry,
                                   root / "education" / "1.26.33.0")

    def test_a_verified_download_replaces_the_build_and_leaves_no_trace(self):
        bundle_bytes = _bundle(_appx(GAME_FILES))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = root / "education" / "1.26.33.0"
            old.mkdir(parents=True)
            (old / "stale.txt").write_text("old")
            dest = self._install(root, bundle_bytes, self._entry(bundle_bytes))

            self.assertTrue((dest / "Minecraft.Windows.exe").is_file())
            self.assertFalse((dest / "stale.txt").exists())
            self.assertEqual(sorted(p.name for p in dest.parent.iterdir()),
                             ["1.26.33.0"])

    def test_a_download_that_does_not_match_is_never_unpacked(self):
        bundle_bytes = _bundle(_appx(GAME_FILES))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = root / "education" / "1.26.33.0"
            old.mkdir(parents=True)
            (old / "kept.txt").write_text("previous build")
            with self.assertRaisesRegex(wustore.WindowsUpdateError,
                                        "does not match"):
                self._install(root, bundle_bytes,
                              self._entry(bundle_bytes, sha256="d3Jvbmc="))
            self.assertEqual((old / "kept.txt").read_text(), "previous build")
            self.assertEqual(sorted(p.name for p in old.parent.iterdir()),
                             ["1.26.33.0"])

    def test_a_build_windows_update_no_longer_lists_is_said_so(self):
        with tempfile.TemporaryDirectory() as td, \
                self.assertRaisesRegex(wustore.WindowsUpdateError,
                                       "not on Windows Update any more"):
            wustore.install(EDUCATION, {"version": "1.26.30.0", "urls": []},
                            Path(td) / "x")

    def test_no_download_location_is_an_error(self):
        bundle_bytes = _bundle(_appx(GAME_FILES))
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(wustore, "file_urls", return_value={}), \
                self.assertRaisesRegex(wustore.WindowsUpdateError,
                                       "no download location"):
            wustore.install(EDUCATION, self._entry(bundle_bytes),
                            Path(td) / "x")


if __name__ == "__main__":
    unittest.main()
