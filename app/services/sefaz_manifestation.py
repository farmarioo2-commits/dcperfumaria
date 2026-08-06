from __future__ import annotations

import base64
import hashlib
import re
import tempfile
from datetime import datetime
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from lxml import etree

NFE_NS = "http://www.portalfiscal.inf.br/nfe"
DS_NS = "http://www.w3.org/2000/09/xmldsig#"
SOAP12_NS = "http://www.w3.org/2003/05/soap-envelope"
EVENT_WSDL_NS = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeRecepcaoEvento4"
PRODUCTION_EVENT_URL = "https://www.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx"
HOMOLOGATION_EVENT_URL = "https://hom1.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx"


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _material(p12_path: str, password: str):
    key, cert, chain = pkcs12.load_key_and_certificates(
        Path(p12_path).read_bytes(),
        password.encode("utf-8") if password else None,
    )
    if key is None or cert is None:
        raise ValueError("Certificado A1 inválido")
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_pem += b"".join(
        item.public_bytes(serialization.Encoding.PEM)
        for item in (chain or [])
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key, cert, cert_pem, key_pem


def _pem_files(p12_path: str, password: str) -> tuple[str, str]:
    _, _, cert_pem, key_pem = _material(p12_path, password)
    cert_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    key_file = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
    cert_file.write(cert_pem)
    key_file.write(key_pem)
    cert_file.close()
    key_file.close()
    return cert_file.name, key_file.name


def _add(parent, name: str, text: str | None = None):
    element = etree.SubElement(parent, etree.QName(NFE_NS, name))
    if text is not None:
        element.text = text
    return element


def _sign(evento, inf_evento, private_key, certificate) -> None:
    canonical_inf = etree.tostring(
        inf_evento,
        method="c14n",
        exclusive=False,
        with_comments=False,
    )
    digest = base64.b64encode(
        hashlib.sha1(canonical_inf).digest()
    ).decode("ascii")

    signature = etree.Element(
        etree.QName(DS_NS, "Signature"),
        nsmap={"ds": DS_NS},
    )
    signed_info = etree.SubElement(
        signature, etree.QName(DS_NS, "SignedInfo")
    )
    etree.SubElement(
        signed_info,
        etree.QName(DS_NS, "CanonicalizationMethod"),
        Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )
    etree.SubElement(
        signed_info,
        etree.QName(DS_NS, "SignatureMethod"),
        Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1",
    )
    reference = etree.SubElement(
        signed_info,
        etree.QName(DS_NS, "Reference"),
        URI=f"#{inf_evento.get('Id')}",
    )
    transforms = etree.SubElement(
        reference, etree.QName(DS_NS, "Transforms")
    )
    etree.SubElement(
        transforms,
        etree.QName(DS_NS, "Transform"),
        Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature",
    )
    etree.SubElement(
        transforms,
        etree.QName(DS_NS, "Transform"),
        Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )
    etree.SubElement(
        reference,
        etree.QName(DS_NS, "DigestMethod"),
        Algorithm="http://www.w3.org/2000/09/xmldsig#sha1",
    )
    etree.SubElement(
        reference, etree.QName(DS_NS, "DigestValue")
    ).text = digest

    signed_info_bytes = etree.tostring(
        signed_info,
        method="c14n",
        exclusive=False,
        with_comments=False,
    )
    signature_bytes = private_key.sign(
        signed_info_bytes,
        padding.PKCS1v15(),
        hashes.SHA1(),
    )
    etree.SubElement(
        signature, etree.QName(DS_NS, "SignatureValue")
    ).text = base64.b64encode(signature_bytes).decode("ascii")

    key_info = etree.SubElement(
        signature, etree.QName(DS_NS, "KeyInfo")
    )
    x509_data = etree.SubElement(
        key_info, etree.QName(DS_NS, "X509Data")
    )
    etree.SubElement(
        x509_data, etree.QName(DS_NS, "X509Certificate")
    ).text = base64.b64encode(
        certificate.public_bytes(serialization.Encoding.DER)
    ).decode("ascii")
    evento.append(signature)


def _envelope(
    cnpj: str,
    access_key: str,
    environment: str,
    p12_path: str,
    password: str,
) -> tuple[str, bytes]:
    cnpj_digits = _digits(cnpj)
    key_digits = _digits(access_key)
    if len(cnpj_digits) != 14:
        raise ValueError("CNPJ inválido")
    if len(key_digits) != 44:
        raise ValueError("Chave de acesso inválida")

    private_key, certificate, _, _ = _material(p12_path, password)
    tp_amb = "1" if environment.upper() == "PRODUCAO" else "2"

    env_evento = etree.Element(
        etree.QName(NFE_NS, "envEvento"),
        nsmap={None: NFE_NS},
    )
    env_evento.set("versao", "1.00")
    _add(
        env_evento,
        "idLote",
        str(int(datetime.now().timestamp() * 1000))[-15:],
    )

    evento = _add(env_evento, "evento")
    evento.set("versao", "1.00")
    inf = _add(evento, "infEvento")
    inf.set("Id", f"ID210210{key_digits}01")
    _add(inf, "cOrgao", "91")
    _add(inf, "tpAmb", tp_amb)
    _add(inf, "CNPJ", cnpj_digits)
    _add(inf, "chNFe", key_digits)
    _add(
        inf,
        "dhEvento",
        datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    _add(inf, "tpEvento", "210210")
    _add(inf, "nSeqEvento", "1")
    _add(inf, "verEvento", "1.00")
    detail = _add(inf, "detEvento")
    detail.set("versao", "1.00")
    _add(detail, "descEvento", "Ciencia da Operacao")
    _sign(evento, inf, private_key, certificate)

    envelope = etree.Element(
        etree.QName(SOAP12_NS, "Envelope"),
        nsmap={"soap12": SOAP12_NS},
    )
    body = etree.SubElement(
        envelope, etree.QName(SOAP12_NS, "Body")
    )
    operation = etree.SubElement(
        body, etree.QName(EVENT_WSDL_NS, "nfeRecepcaoEvento")
    )
    message = etree.SubElement(
        operation, etree.QName(EVENT_WSDL_NS, "nfeDadosMsg")
    )
    message.append(env_evento)

    url = (
        PRODUCTION_EVENT_URL
        if tp_amb == "1"
        else HOMOLOGATION_EVENT_URL
    )
    return url, etree.tostring(
        envelope,
        xml_declaration=True,
        encoding="utf-8",
    )


def _parse(content: bytes) -> dict:
    root = etree.fromstring(content)
    info = root.find(
        f".//{{{NFE_NS}}}retEvento/{{{NFE_NS}}}infEvento"
    )
    if info is None:
        top = root.find(f".//{{{NFE_NS}}}retEnvEvento")
        if top is None:
            raise ValueError("Resposta inesperada da SEFAZ")
        return {
            "accepted": False,
            "status_code": top.findtext(
                f"{{{NFE_NS}}}cStat", default=""
            ),
            "status_message": top.findtext(
                f"{{{NFE_NS}}}xMotivo", default=""
            ),
        }

    code = info.findtext(f"{{{NFE_NS}}}cStat", default="")
    return {
        "accepted": code in {"135", "136", "573"},
        "status_code": code,
        "status_message": info.findtext(
            f"{{{NFE_NS}}}xMotivo", default=""
        ),
        "protocol": info.findtext(
            f"{{{NFE_NS}}}nProt", default=""
        ),
    }


def manifest_science(
    cnpj: str,
    access_key: str,
    environment: str,
    p12_path: str,
    password: str,
) -> dict:
    url, envelope = _envelope(
        cnpj,
        access_key,
        environment,
        p12_path,
        password,
    )
    cert_file, key_file = _pem_files(p12_path, password)
    try:
        headers = {
            "Content-Type": (
                "application/soap+xml; charset=utf-8; "
                f'action="{EVENT_WSDL_NS}/nfeRecepcaoEvento"'
            )
        }
        with httpx.Client(
            cert=(cert_file, key_file),
            timeout=httpx.Timeout(90.0, connect=30.0),
            follow_redirects=True,
        ) as client:
            response = client.post(
                url,
                content=envelope,
                headers=headers,
            )
            response.raise_for_status()
            return _parse(response.content)
    finally:
        Path(cert_file).unlink(missing_ok=True)
        Path(key_file).unlink(missing_ok=True)
