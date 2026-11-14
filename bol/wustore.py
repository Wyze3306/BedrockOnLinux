"""bol.wustore — Store apps that Windows Update serves as plain packages.

Minecraft for Windows and its Preview are Xbox titles: an encrypted MSIXVC on
the Xbox CDN, which only an account that owns the game can get the key for,
and Xodus is what gets it (bol.xodus). Minecraft Education is not one of them
(issue #351). The Store catalogue lists it as an ordinary AppxBundle with a
Windows Update category, it is free to install, and the account check is the
game's own: it opens a Microsoft 365 sign-in for a school or work account
before anything can be played. Xodus has nothing to offer here, and asking it
for the product ends in "Package was not found".

What Windows itself does for such an app is ask Windows Update's Store front
end (FE3) for the updates of that category, then for the download location of
the package, and fetch it from Microsoft's delivery network. That is what this
module does, over the same SOAP endpoint, without signing anyone in: FE3
answers anonymous requests for a free app the way it answers a fresh Windows
install. The file comes over plain HTTP, as it does for Windows, so it is
checked against the SHA-256 the (TLS-protected) FE3 answer gave for it before
anything is unpacked.

FE3 itself is served under Microsoft's own PKI -- "Microsoft Root Certificate
Authority 2011", which no Linux CA bundle carries -- so that one root, and
nothing else, is what those requests are verified against.

The package is a zip holding a zip: the bundle carries one application
package per architecture, and the x64 one holds the game. The delivery
network honours byte ranges, so the version of the game inside a bundle is
read from the bundle's manifest with two small requests rather than by
downloading it -- which is what lets the launcher name a build ("1.26.33")
before it has it.
"""
# SPDX-License-Identifier: MIT

import base64
import datetime
import hashlib
import io
import json
import shutil
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

from .config import APP, CACHE
from .log import BolError, info, ok
from .util import download

FE3 = "https://fe3.delivery.mp.microsoft.com/ClientWebService/client.asmx"
FE3_SECURED = FE3 + "/secured"

# Microsoft Root Certificate Authority 2011 (SHA-1 8F43288AD272F3103B6FB142
# 8485EA3014C0BCFE), the root FE3's "Microsoft Update Secure Server CA 2.1"
# chains to. Public, and only ever trusted for the FE3 requests below.
MICROSOFT_ROOT_2011 = """-----BEGIN CERTIFICATE-----
MIIF7TCCA9WgAwIBAgIQP4vItfyfspZDtWnWbELhRDANBgkqhkiG9w0BAQsFADCB
iDELMAkGA1UEBhMCVVMxEzARBgNVBAgTCldhc2hpbmd0b24xEDAOBgNVBAcTB1Jl
ZG1vbmQxHjAcBgNVBAoTFU1pY3Jvc29mdCBDb3Jwb3JhdGlvbjEyMDAGA1UEAxMp
TWljcm9zb2Z0IFJvb3QgQ2VydGlmaWNhdGUgQXV0aG9yaXR5IDIwMTEwHhcNMTEw
MzIyMjIwNTI4WhcNMzYwMzIyMjIxMzA0WjCBiDELMAkGA1UEBhMCVVMxEzARBgNV
BAgTCldhc2hpbmd0b24xEDAOBgNVBAcTB1JlZG1vbmQxHjAcBgNVBAoTFU1pY3Jv
c29mdCBDb3Jwb3JhdGlvbjEyMDAGA1UEAxMpTWljcm9zb2Z0IFJvb3QgQ2VydGlm
aWNhdGUgQXV0aG9yaXR5IDIwMTEwggIiMA0GCSqGSIb3DQEBAQUAA4ICDwAwggIK
AoICAQCygEGqNThNE3IyaCJNuLLx/9VSvGzH9dJKjDbu0cJcfoyKrq8TKG/Ac+M6
ztAlqFo6be+ouFmrEyNozQwph9FvgFyPRH9dkAFSWKxRxV8qh9zc2AodwQO5e7BW
6KPeZGHCnvjzfLnsDbVU/ky2ZU+I8JxImQxCCwl8MVkXeQZ4KI2JOkwDJb5xalwL
54RgpJki49KvhKSn+9GY7Qyp3pSJ4Q6g3MDOmT3qCFK7VnnkH4S6Hri0xElcTzFL
h93dBWcmmYDgcRGjuKVB4qRTufcyKYMME782XgSzS0NHL2vikR7TmE/dQgfI6B0S
/Jmpaz6SfsjWaTr8ZL22CZ3K/QwLopt3YEsDlKQwaRLWQi3BQUzK3Kr9j1uDRprZ
/LHR47PJf0h6zSTwQY9cdNCssBAgBkm3xy0hyFfj0IbzA2j70M5xwYmZSmQBbP3s
MJHPQTySx+W6hh1hhMdfgzlirrSSL0fzC/hV66AfWdC7dJse0Hbm8ukG1xDo+mTe
acY1logC8Ea4PyeZb8txiSk190gWAjWP1Xl8TQLPX+uKg09FcYj5qQ1OcunCnAfP
SRtOBA5jUYxe2ADBVSy2xuDCZU7JNDn1nLPEfuhhbhNfFcRf2X7tHc7uROzLLoax
7Dj2cO2rXBPB2Q8Nx4CyVe0096yb5MPa50c8prWPMd/FS6/r8QIDAQABo1EwTzAL
BgNVHQ8EBAMCAYYwDwYDVR0TAQH/BAUwAwEB/zAdBgNVHQ4EFgQUci06AjGQQ7kU
BU7h6qfHMdEjiTQwEAYJKwYBBAGCNxUBBAMCAQAwDQYJKoZIhvcNAQELBQADggIB
AH9yzw+3xRXbm8BJyiZb/p4T5tPw0tuXX/JLP02zrhmu7deXoKzvqTqjwkGw5biR
nhOBJAPmCf0/V0A5ISRW0RAvS0CpNoZLtFNXmvvxfomPEf4YbFGq6O0JlbXlccmh
6Yd1phV/yX43VF50k8XDZ8wNT2uoFwxtCJJ+i92Bqi1wIcM9BhS7vyRep4TXPw8h
Ir1LAAbblxzYXtTFC1yHblCk6MM4pPvLLMWSZpuFXst6bJN8gClYW1e1QGm6CHmm
ZGIVnYeWRbVmIyADixxzoNOieTPgUFmG2y/lAiXqcyqfABTINseSO+lOAOzYVgm5
M0kS0lQLAausR7aRKX1MtHWAUgHoyoL2n8ysnI8X6i8msKtyrAv+nlEex0NVZ09R
s1fWtuzuUrc66U7h14GIvE+OdbtLqPA1qibUZ2dJsnBMO5PcHd94kIZysjik0dyS
TclY6ysSXNQ7roxrsIPlAT/4CTL2kzU0Iq/dNw13CYArzUgA8YyZGUcFAenRv9FO
0OYoQzeZpApKCNmacXPSqs0xE2N2oTdvkjgefRI8ZjLny23h/FKJ3crWZgWalmG+
oijHHKOnNlA8OqTfSm7mhzvO6/DggTedEzxSjr25HTTGHdUKaj2YKXCMiSrRq4IQ
SB/c9O+lxbtVGjhjhE63bK2VVOxlIhBJF7jAHscPrFRH
-----END CERTIFICATE-----
"""

_NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
_NS_ADDR = "http://www.w3.org/2005/08/addressing"
_NS_WSSE = ("http://docs.oasis-open.org/wss/2004/01/"
            "oasis-200401-wss-wssecurity-secext-1.0.xsd")
_NS_WSU = ("http://docs.oasis-open.org/wss/2004/01/"
           "oasis-200401-wss-wssecurity-utility-1.0.xsd")
_NS_WUA = "http://schemas.microsoft.com/msus/2014/10/WindowsUpdateAuthorization"
_NS_CWS = "http://www.microsoft.com/SoftwareDistribution/Server/ClientWebService"
_ACTION = "http://www.microsoft.com/SoftwareDistribution/Server/ClientWebService/"

# The device FE3 is told it is answering: a current, retail Windows 11 x64
# desktop. Applicability rules are evaluated against it, so it decides which
# packages come back -- an x64 desktop is what the game is built for.
_DEVICE_ATTRIBUTES = "&".join((
    "E:BranchReadinessLevel=CB", "CurrentBranch=rs_prerelease",
    "OEMModel=Virtual%20Machine", "FlightRing=Retail", "AttrDataVer=21",
    "SystemManufacturer=Microsoft%20Corporation", "InstallLanguage=en-US",
    "OSUILocale=en-US", "InstallationType=Client",
    "FlightingBranchName=external",
    "FirmwareVersion=Hyper-V%20UEFI%20Release%20v2.5",
    "SystemProductName=Virtual%20Machine", "OSSkuId=48",
    "FlightContent=Branch", "App=WU",
    "OEMName_Uncleaned=Microsoft%20Corporation", "AppVer=10.0.22621.900",
    "OSArchitecture=AMD64", "SystemSKU=None", "UpdateManagementGroup=2",
    "IsFlightingEnabled=1", "IsDeviceRetailDemo=0", "TelemetryLevel=3",
    "OSVersion=10.0.22621.900", "DeviceFamily=Windows.Desktop"))

# The detectoids a Windows install reports as already evaluated. FE3 only
# returns the updates whose prerequisites are in this list, so without it the
# answer holds the category and none of the packages in it.
_INSTALLED_NON_LEAF = (
    1, 2, 3, 11, 19, 544, 549, 2359974, 2359977, 5169044, 8788830, 23110993,
    23110994, 54341900, 54343656, 59830006, 59830007, 59830008, 60484010,
    62450018, 62450019, 62450020, 66027979, 66053150, 97657898, 98822896,
    98959022, 98959023, 98959024, 98959025, 98959026, 104433538, 104900364,
    105489019, 117765322, 129905029, 130040031, 132387090, 132393049,
    133399034, 138537048, 140377312, 143747671, 158941041, 158941042,
    158941043, 158941044, 159123858, 159130928, 164836897, 164847386,
    164848327, 164852241, 164852246, 164852252, 164852253)

_PACKAGE_SUFFIXES = (".appxbundle", ".msixbundle", ".appx", ".msix")
# Package metadata that is not part of the app; the manifest stays, since it
# is what marks a finished install (bol.xodus.game_root).
_PACKAGE_METADATA = {"appxblockmap.xml", "appxsignature.p7x",
                     "[content_types].xml"}
_CATALOGUE_TTL = 12 * 3600
_TIMEOUT = 60
_BLOCK = 64 << 10


class WindowsUpdateError(BolError):
    """Windows Update could not list or deliver a package."""


# ------------------------------------------------------------------ SOAP


def _ssl_context():
    """A TLS context that trusts Microsoft's 2011 root and nothing else."""
    context = ssl.create_default_context(cadata=MICROSOFT_ROOT_2011)
    return context


def _envelope(action, to, body, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    created = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    expires = (now + datetime.timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    return (
        f'<s:Envelope xmlns:a="{_NS_ADDR}" xmlns:s="{_NS_SOAP}"><s:Header>'
        f'<a:Action s:mustUnderstand="1">{_ACTION}{action}</a:Action>'
        f'<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>'
        f'<a:To s:mustUnderstand="1">{to}</a:To>'
        f'<o:Security s:mustUnderstand="1" xmlns:o="{_NS_WSSE}">'
        f'<Timestamp xmlns="{_NS_WSU}"><Created>{created}</Created>'
        f'<Expires>{expires}</Expires></Timestamp>'
        f'<wuws:WindowsUpdateTicketsToken wsu:id="ClientMSA" '
        f'xmlns:wsu="{_NS_WSU}" xmlns:wuws="{_NS_WUA}">'
        f'<TicketType Name="MSA" Version="1.0" Policy="MBI_SSL"><User/>'
        f'</TicketType></wuws:WindowsUpdateTicketsToken></o:Security>'
        f'</s:Header><s:Body>{body}</s:Body></s:Envelope>')


def _post(url, action, body, opener=None):
    """POST one SOAP request to FE3 and return the parsed envelope."""
    data = _envelope(action, url, body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/soap+xml; charset=utf-8",
        "User-Agent": APP})
    try:
        if opener is not None:
            response = opener(request)
        else:
            response = urllib.request.urlopen(
                request, timeout=_TIMEOUT, context=_ssl_context())
        with response:
            payload = response.read()
    except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
        raise WindowsUpdateError(
            f"Windows Update could not be reached ({exc}).") from exc
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise WindowsUpdateError(
            f"Windows Update sent an answer that is not XML ({exc}).") from exc


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find_all(root, name):
    return [el for el in root.iter() if _local(el.tag) == name]


def _find(root, name):
    return next((el for el in root.iter() if _local(el.tag) == name), None)


def _fragment(text):
    """An escaped XML fragment FE3 nests in its answer, parsed."""
    try:
        return ET.fromstring(f"<fragment>{text or ''}</fragment>")
    except ET.ParseError:
        return None


def _cookie(opener=None):
    body = (f'<GetCookie xmlns="{_NS_CWS}"><oldCookie></oldCookie>'
            '<lastChange>2015-10-21T17:01:07.1472913Z</lastChange>'
            '<currentTime>2017-12-02T00:16:15.217Z</currentTime>'
            '<protocolVersion>1.40</protocolVersion></GetCookie>')
    root = _post(FE3, "GetCookie", body, opener)
    data = _find(root, "EncryptedData")
    if data is None or not (data.text or "").strip():
        raise WindowsUpdateError("Windows Update did not open a session.")
    return data.text.strip()


def sync_body(category_id, cookie):
    ids = "".join(f"<int>{i}</int>" for i in _INSTALLED_NON_LEAF)
    return (
        f'<SyncUpdates xmlns="{_NS_CWS}"><cookie>'
        f'<Expiration>2045-03-11T02:02:48Z</Expiration>'
        f'<EncryptedData>{cookie}</EncryptedData></cookie><parameters>'
        f'<ExpressQuery>false</ExpressQuery>'
        f'<InstalledNonLeafUpdateIDs>{ids}</InstalledNonLeafUpdateIDs>'
        f'<OtherCachedUpdateIDs/><SkipSoftwareSync>false</SkipSoftwareSync>'
        f'<NeedTwoGroupOutOfScopeUpdates>true</NeedTwoGroupOutOfScopeUpdates>'
        f'<FilterAppCategoryIds><CategoryIdentifier><Id>{category_id}</Id>'
        f'</CategoryIdentifier></FilterAppCategoryIds>'
        f'<TreatAppCategoryIdsAsInstalled>true</TreatAppCategoryIdsAsInstalled>'
        f'<AlsoPerformRegularSync>false</AlsoPerformRegularSync>'
        f'<ComputerSpec/><ExtendedUpdateInfoParameters><XmlUpdateFragmentTypes>'
        f'<XmlUpdateFragmentType>Extended</XmlUpdateFragmentType>'
        f'</XmlUpdateFragmentTypes><Locales><string>en-US</string>'
        f'<string>en</string></Locales></ExtendedUpdateInfoParameters>'
        f'<ClientPreferredLanguages><string>en-US</string>'
        f'</ClientPreferredLanguages><ProductsParameters>'
        f'<SyncCurrentVersionOnly>false</SyncCurrentVersionOnly>'
        f'<DeviceAttributes>{_xml_text(_DEVICE_ATTRIBUTES)}</DeviceAttributes>'
        f'<CallerAttributes>Interactive=1;IsSeeker=0;</CallerAttributes>'
        f'<Products/></ProductsParameters></parameters></SyncUpdates>')


def _xml_text(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def parse_sync(root):
    """The packages a SyncUpdates answer offers, one dict per file.

    Two halves of the answer have to be joined: ``NewUpdates`` names each
    update's identity (the id and revision a download location is asked for
    with), and ``ExtendedUpdateInfo`` names its files, their digests and the
    package moniker. They share FE3's numeric update id.
    """
    identities = {}
    for update in _find_all(root, "UpdateInfo"):
        number = _find(update, "ID")
        fragment = _fragment(getattr(_find(update, "Xml"), "text", ""))
        if number is None or fragment is None:
            continue
        identity = _find(fragment, "UpdateIdentity")
        if identity is None:
            continue
        identities[(number.text or "").strip()] = (
            identity.get("UpdateID"), identity.get("RevisionNumber"))
    packages = []
    for update in _find_all(root, "Update"):
        number = _find(update, "ID")
        fragment = _fragment(getattr(_find(update, "Xml"), "text", ""))
        if number is None or fragment is None:
            continue
        identity = identities.get((number.text or "").strip())
        if not identity or not all(identity):
            continue
        for item in _find_all(fragment, "File"):
            name = item.get("FileName") or ""
            moniker = item.get("InstallerSpecificIdentifier") or ""
            if not moniker or not name.lower().endswith(_PACKAGE_SUFFIXES):
                continue
            sha256 = next((d.text for d in _find_all(item, "AdditionalDigest")
                           if (d.get("Algorithm") or "").upper() == "SHA256"),
                          None)
            try:
                size = int(item.get("Size") or 0)
            except ValueError:
                size = 0
            packages.append({
                "update_id": identity[0], "revision": identity[1],
                "file_name": name, "moniker": moniker, "size": size,
                "sha1": item.get("Digest"), "sha256": (sha256 or "").strip(),
            })
    return packages


def sync_packages(category_id, opener=None):
    """Every package Windows Update lists in a Store app's category."""
    cookie = _cookie(opener)
    root = _post(FE3, "SyncUpdates", sync_body(category_id, cookie), opener)
    return parse_sync(root)


def file_urls(update_id, revision, opener=None):
    """Download locations for one update, keyed by the file's SHA-1 digest.

    They are signed and short-lived -- hours, not days -- so they are asked
    for right before a download rather than kept with the catalogue.
    """
    body = (f'<GetExtendedUpdateInfo2 xmlns="{_NS_CWS}"><updateIDs>'
            f'<UpdateIdentity><UpdateID>{update_id}</UpdateID>'
            f'<RevisionNumber>{revision}</RevisionNumber></UpdateIdentity>'
            f'</updateIDs><infoTypes>'
            f'<XmlUpdateFragmentType>FileUrl</XmlUpdateFragmentType>'
            f'<XmlUpdateFragmentType>FileDecryption</XmlUpdateFragmentType>'
            f'</infoTypes><deviceAttributes>{_xml_text(_DEVICE_ATTRIBUTES)}'
            f'</deviceAttributes></GetExtendedUpdateInfo2>')
    root = _post(FE3_SECURED, "GetExtendedUpdateInfo2", body, opener)
    out = {}
    for location in _find_all(root, "FileLocation"):
        digest = _find(location, "FileDigest")
        url = _find(location, "Url")
        if digest is None or url is None or not (url.text or "").strip():
            continue
        out[(digest.text or "").strip()] = url.text.strip()
    return out


# ------------------------------------------------------------- packages


def parse_moniker(moniker):
    """``Name_Version_Arch_ResourceId_PublisherId`` as a dict, or None."""
    parts = str(moniker).split("_")
    if len(parts) != 5:
        return None
    name, version, arch, _resource, publisher = parts
    return {"name": name, "version": version, "arch": arch,
            "family": f"{name}_{publisher}"}


def version_key(version):
    out = []
    for piece in str(version).split("."):
        try:
            out.append(int(piece))
        except ValueError:
            out.append(0)
    return tuple(out)


def newest_package(packages, family):
    """The newest main package of ``family`` -- a bundle when there is one."""
    best = None
    for package in packages:
        parsed = parse_moniker(package["moniker"])
        if not parsed or parsed["family"].lower() != family.lower():
            continue
        if parsed["arch"] not in ("neutral", "x64"):
            continue
        rank = (version_key(parsed["version"]),
                package["file_name"].lower().endswith(("bundle",)))
        if best is None or rank > best[0]:
            best = (rank, dict(package, package_version=parsed["version"]))
    return best[1] if best else None


def game_version(appx_version):
    """Mojang's number for a Minecraft package version.

    The package keeps "<minor><patch>" in its third field with the patch on
    the last two digits -- 1.26.3300.0 is Minecraft 1.26.33 -- the same
    packing bol.games.mc_version_str undoes for an installed build.
    """
    parts = str(appx_version).split(".")
    if len(parts) < 3 or not all(p.isdigit() for p in parts[:3]):
        return str(appx_version)
    major, minor, packed = parts[0], parts[1], parts[2]
    if len(packed) >= 3:
        return f"{int(major)}.{int(minor)}.{int(packed[:-2])}.{int(packed[-2:])}"
    return f"{int(major)}.{int(minor)}.{int(packed)}"


def bundle_application(manifest, arch="x64"):
    """(file name, version) of the application package a bundle carries.

    ``manifest`` is AppxMetadata/AppxBundleManifest.xml. A bundle lists one
    application package per architecture beside resource packages; a plain
    package bundled alone is listed as "neutral".
    """
    try:
        root = ET.fromstring(manifest)
    except ET.ParseError as exc:
        raise WindowsUpdateError(
            f"The package's bundle manifest cannot be read ({exc}).") from exc
    candidates = [p for p in _find_all(root, "Package")
                  if (p.get("Type") or "").lower() == "application"]
    for wanted in (arch, "neutral"):
        for package in candidates:
            if (package.get("Architecture") or "").lower() == wanted:
                return package.get("FileName"), package.get("Version")
    raise WindowsUpdateError(
        f"The package has no {arch} version of the game in it.")


class _RangeReader(io.RawIOBase):
    """A read-only, seekable view of a remote file through HTTP ranges.

    Enough of a file for zipfile to find a zip's central directory and read
    one member out of it: reads are served from 64 KiB blocks, so the handful
    of small reads zipfile makes cost a handful of requests.
    """

    def __init__(self, url, size, fetch=None):
        super().__init__()
        self._url = url
        self._size = int(size)
        self._pos = 0
        self._blocks = {}
        self._fetch = fetch or self._http_range

    def _http_range(self, start, end):
        request = urllib.request.Request(self._url, headers={
            "Range": f"bytes={start}-{end}", "User-Agent": APP})
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            if getattr(response, "status", 206) != 206:
                raise WindowsUpdateError(
                    "The download server does not serve parts of a file.")
            return response.read()

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self._size + offset
        self._pos = max(0, self._pos)
        return self._pos

    def _block(self, index):
        if index not in self._blocks:
            start = index * _BLOCK
            end = min(self._size, start + _BLOCK) - 1
            data = self._fetch(start, end)
            if len(data) != end - start + 1:
                raise WindowsUpdateError(
                    "The download server sent part of a file short.")
            self._blocks[index] = data
        return self._blocks[index]

    def readinto(self, buffer):
        view = memoryview(buffer)
        want = min(len(view), max(0, self._size - self._pos))
        done = 0
        while done < want:
            index, offset = divmod(self._pos, _BLOCK)
            block = self._block(index)
            take = min(want - done, len(block) - offset)
            view[done:done + take] = block[offset:offset + take]
            done += take
            self._pos += take
        return done


class _Slice(io.RawIOBase):
    """``length`` bytes of a seekable file from ``offset``, as a file."""

    def __init__(self, handle, offset, length):
        super().__init__()
        self._handle = handle
        self._offset = offset
        self._length = length
        self._pos = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=io.SEEK_SET):
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos,
                io.SEEK_END: self._length}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def readinto(self, buffer):
        want = min(len(buffer), max(0, self._length - self._pos))
        if not want:
            return 0
        self._handle.seek(self._offset + self._pos)
        data = self._handle.read(want)
        buffer[:len(data)] = data
        self._pos += len(data)
        return len(data)


def _member_data_offset(handle, info):
    """Where a zip member's data starts: past its local header."""
    handle.seek(info.header_offset)
    header = handle.read(30)
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
        raise WindowsUpdateError("The package is not a valid zip archive.")
    name_length, extra_length = struct.unpack("<HH", header[26:30])
    return info.header_offset + 30 + name_length + extra_length


def _application_member(bundle, arch="x64"):
    """The ZipInfo of the application package inside an open bundle."""
    try:
        manifest = bundle.read("AppxMetadata/AppxBundleManifest.xml")
    except KeyError:
        return None, None
    name, version = bundle_application(manifest, arch)
    try:
        return bundle.getinfo(name), version
    except KeyError as exc:
        raise WindowsUpdateError(
            f"The package lists {name} but does not carry it.") from exc


def remote_game_version(url, size, fetch=None):
    """The version of the game inside a remote bundle, read in place."""
    reader = io.BufferedReader(_RangeReader(url, size, fetch), _BLOCK)
    try:
        with zipfile.ZipFile(reader) as bundle:
            _info, version = _application_member(bundle)
    except zipfile.BadZipFile as exc:
        raise WindowsUpdateError(
            f"The package is not a valid zip archive ({exc}).") from exc
    if version is None:
        raise WindowsUpdateError("The package is not a bundle.")
    return game_version(version)


# ------------------------------------------------------------ catalogue


def _cache_path(edition):
    return Path(CACHE) / f"wu-{edition['id']}.json"


def _read_cache(edition, ttl):
    path = _cache_path(edition)
    try:
        if ttl is not None and time.time() - path.stat().st_mtime >= ttl:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return payload if isinstance(payload, list) else None


def _write_cache(edition, catalogue):
    path = _cache_path(edition)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(path.name + ".tmp")
        staged.write_text(json.dumps(catalogue), encoding="utf-8")
        staged.replace(path)
    except OSError:
        pass


def latest_build(edition, opener=None, fetch=None):
    """The current build of a Windows Update edition, as a catalogue entry.

    Windows Update offers one build -- the current one -- like the Xbox CDN
    does for the other editions, so a catalogue here has one entry.
    """
    packages = sync_packages(edition["wu_category"], opener)
    package = newest_package(packages, edition["family"])
    if package is None:
        raise WindowsUpdateError(
            f"Windows Update lists no {edition['name']} package.")
    urls = file_urls(package["update_id"], package["revision"], opener)
    url = urls.get(package["sha1"])
    if not url:
        raise WindowsUpdateError(
            f"Windows Update gave no download location for {edition['name']}.")
    if package["file_name"].lower().endswith(("bundle",)):
        version = remote_game_version(url, package["size"], fetch)
    else:
        version = game_version(package["package_version"])
    return {"version": version, "urls": [], "package": package}


def version_catalogue(edition, ignore_cache=False, opener=None, fetch=None):
    """``[{"version", "urls", "package"}]`` for a Windows Update edition.

    Kept for twelve hours like the build index of the other editions; when
    Windows Update cannot be reached, the copy kept last time is listed
    rather than nothing.
    """
    if not ignore_cache:
        cached = _read_cache(edition, _CATALOGUE_TTL)
        if cached:
            return cached
    try:
        catalogue = [latest_build(edition, opener, fetch)]
    except WindowsUpdateError:
        stale = _read_cache(edition, None)
        if stale:
            return stale
        raise
    _write_cache(edition, catalogue)
    return catalogue


# -------------------------------------------------------------- install


def _free_space(path):
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return None


def _sha256_b64(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return base64.b64encode(digest.digest()).decode("ascii")


def _safe_member_path(name):
    """A package member's path inside the install, or None to skip it.

    Package members are percent-encoded ("Assets/My%20Logo.png"), and a
    member is never allowed to leave the folder it is unpacked into.
    """
    decoded = urllib.parse.unquote(name).replace("\\", "/")
    path = PurePosixPath(decoded)
    if (not decoded or decoded.endswith("/") or path.is_absolute()
            or any(part in ("", "..") for part in path.parts)
            or ":" in path.parts[0]):
        return None
    if len(path.parts) == 1 and path.name.lower() in _PACKAGE_METADATA:
        return None
    return path


def unpack_package(package_file, dest, progress=None):
    """Unpack the game a downloaded bundle (or plain package) carries.

    The application package inside a bundle is stored, not compressed, so
    it is read in place from the bundle instead of being copied out first:
    the install never needs room for a third copy of the game.
    """
    package_file = Path(package_file)
    dest = Path(dest)
    with open(package_file, "rb") as raw:
        try:
            with zipfile.ZipFile(raw) as outer:
                member, _version = _application_member(outer)
                if member is None:
                    _unpack_zip(outer, dest, progress)
                    return
                if member.compress_type != zipfile.ZIP_STORED:
                    raise WindowsUpdateError(
                        "The game package inside the bundle is compressed, "
                        "which Microsoft's packages never are.")
                start = _member_data_offset(raw, member)
                inner_file = io.BufferedReader(
                    _Slice(raw, start, member.file_size), 1 << 20)
                with zipfile.ZipFile(inner_file) as inner:
                    _unpack_zip(inner, dest, progress)
        except zipfile.BadZipFile as exc:
            raise WindowsUpdateError(
                f"The downloaded package is not a valid archive ({exc}).") \
                from exc


def _unpack_zip(archive, dest, progress=None):
    members = sorted(archive.infolist(), key=lambda i: i.header_offset)
    wanted = [(member, _safe_member_path(member.filename)) for member in members]
    wanted = [(member, path) for member, path in wanted if path is not None]
    total = sum(member.file_size for member, _path in wanted)
    free = _free_space(dest.parent)
    if free is not None and free < total + (64 << 20):
        raise WindowsUpdateError(
            f"Not enough free space to unpack the game: it takes "
            f"{total / (1 << 30):.1f} GiB and {free / (1 << 30):.1f} GiB is "
            f"free on {dest.parent}.")
    done = 0
    for member, path in wanted:
        target = dest.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, open(target, "wb") as sink:
            shutil.copyfileobj(source, sink, 1 << 20)
        done += member.file_size
        if progress and total:
            progress(done, total)


def install(edition, entry, dest, progress=None, opener=None):
    """Download the build ``entry`` names and unpack it into ``dest``.

    The download lands beside ``dest`` and is checked against the SHA-256
    Windows Update published for it before anything is unpacked; the game is
    unpacked into a staging folder that only replaces ``dest`` once it is
    complete, so a failure at any point leaves whatever was there before.
    """
    dest = Path(dest)
    package = (entry or {}).get("package") or {}
    if not package.get("update_id"):
        raise WindowsUpdateError(
            f"{edition['name']} {entry.get('version')} is not on Windows "
            "Update any more.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    urls = file_urls(package["update_id"], package["revision"], opener)
    url = urls.get(package.get("sha1"))
    if not url:
        raise WindowsUpdateError(
            f"Windows Update gave no download location for {edition['name']}.")

    size = int(package.get("size") or 0)
    free = _free_space(dest.parent)
    # The package, and the game unpacked out of it beside it: a game
    # package compresses to a little under half its size.
    needed = size * 3
    if free is not None and size and free < needed:
        raise WindowsUpdateError(
            f"Not enough free space to install {edition['name']}: it needs "
            f"about {needed / (1 << 30):.1f} GiB and "
            f"{free / (1 << 30):.1f} GiB is free on {dest.parent}.")

    suffix = Path(package.get("file_name") or "package.appxbundle").suffix
    download_path = dest.parent / f".{dest.name}{suffix}"
    staging = dest.parent / f".{dest.name}.unpacking"
    retired = dest.parent / f".{dest.name}.old"
    try:
        download(url, download_path, f"{edition['name']} {entry['version']}",
                 progress)
        expected = package.get("sha256")
        if expected and _sha256_b64(download_path) != expected:
            raise WindowsUpdateError(
                f"The downloaded {edition['name']} package does not match "
                "the one Windows Update published, so it was not installed. "
                "Try again.")
        info(f"Unpacking {edition['name']} {entry['version']} …")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        unpack_package(download_path, staging, progress)
        shutil.rmtree(retired, ignore_errors=True)
        if dest.exists():
            dest.replace(retired)
        staging.replace(dest)
    finally:
        # A download cut short stays as ".part" for the next attempt to
        # resume: the location changes, the file it names does not.
        download_path.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(retired, ignore_errors=True)
    ok(f"{edition['name']} {entry['version']} unpacked")
    return dest
