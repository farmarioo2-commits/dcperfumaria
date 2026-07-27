from __future__ import annotations

import base64
import gzip
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

PRODUCTION_URL = "https://www1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx"
HOMOLOGATION_URL = "https://hom.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx"
NFE_NAMESPACE = "http://www.portalfiscal.inf.br/nfe"
WSDL_NAMESPACE = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe"
SOAP12_NAMESPACE = "http://www.w3.org/2003/05/soap-envelope"
NFE_NS = {"nfe": NFE_NAMESPACE}


@dataclass(frozen=True)
class CertificateInfo:
    subject: str
    issuer: str
    serial_number: str
    valid_from: datetime
    valid_until: datetime


def load_certificate_info(path: str, password: str) -> CertificateInfo:
    key, certificate, _ = pkcs12.load_key_and_certificates(
        Path(path).read_bytes(),
        password.encode("utf-8") if password else None,
    )
    if not key or not certificate:
        raise ValueError("Certificado A1 inválido")
    return CertificateInfo(
        subject=certificate.subject.rfc4514_string(),
        issuer=certificate.issuer.rfc4514_string(),
        serial_number=str(certificate.serial_number),
        valid_from=certificate.not_valid_before_utc.replace(tzinfo=None),
        valid_until=certificate.not_valid_after_utc.replace(tzinfo=None),
    )


def _temporary_pem_files(path: str, password: str) -> tuple[str, str]:
    key, certificate, chain = pkcs12.load_key_and_certificates(
        Path(path).read_bytes(),
        password.encode("utf-8") if password else None,
    )
    if not key or not certificate:
        raise ValueError("Certificado A1 inválido")

    certificate_bytes = certificate.public_bytes(serialization.Encoding.PEM)
    certificate_bytes += b"".join(
        item.public_bytes(serialization.Encoding.PEM) for item in (chain or [])
    )
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )

    certificate_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    key_file = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
    try:
        certificate_file.write(certificate_bytes)
        key_file.write(key_bytes)
    finally:
        certificate_file.close()
        key_file.close()
    return certificate_file.name, key_file.name


def _build_query(
    *,
    cnpj: str,
    uf_code: int,
    environment: str,
    mode: Literal["distNSU", "consChNFe"],
    last_nsu: str = "000000000000000",
    access_key: str = "",
) -> tuple[str, str]:
    digits = re.sub(r"\D", "", cnpj or "")
    if len(digits) != 14:
        raise ValueError("CNPJ inválido")

    tp_amb = "1" if environment.upper() == "PRODUCAO" else "2"
    if mode == "distNSU":
        query_body = (
            "<distNSU><ultNSU>"
            + str(last_nsu or "0").zfill(15)
            + "</ultNSU></distNSU>"
        )
    else:
        key_digits = re.sub(r"\D", "", access_key or "")
        if len(key_digits) != 44:
            raise ValueError("A chave de acesso deve conter 44 números")
        query_body = f"<consChNFe><chNFe>{key_digits}</chNFe></consChNFe>"

    distribution_xml = (
        f'<distDFeInt xmlns="{NFE_NAMESPACE}" versao="1.01">'
        f"<tpAmb>{tp_amb}</tpAmb>"
        f"<cUFAutor>{uf_code}</cUFAutor>"
        f"<CNPJ>{digits}</CNPJ>"
        f"{query_body}"
        "</distDFeInt>"
    )
    soap_xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap12:Envelope xmlns:soap12="{SOAP12_NAMESPACE}">'
        "<soap12:Body>"
        f'<nfeDistDFeInteresse xmlns="{WSDL_NAMESPACE}">'
        f"<nfeDadosMsg>{distribution_xml}</nfeDadosMsg>"
        "</nfeDistDFeInteresse>"
        "</soap12:Body>"
        "</soap12:Envelope>"
    )
    url = PRODUCTION_URL if tp_amb == "1" else HOMOLOGATION_URL
    return url, soap_xml


def _locate_distribution_result(response_content: bytes) -> ET.Element:
    try:
        root = ET.fromstring(response_content)
    except ET.ParseError as exc:
        raise ValueError(f"Resposta XML inválida da SEFAZ: {exc}") from exc

    direct = root.find(f".//{{{NFE_NAMESPACE}}}retDistDFeInt")
    if direct is not None:
        return direct

    # Alguns servidores ASMX encapsulam o XML retornado como texto no elemento Result.
    for node in root.iter():
        if node.tag.split("}")[-1] != "nfeDistDFeInteresseResult":
            continue
        text = (node.text or "").strip()
        if not text:
            continue
        try:
            nested = ET.fromstring(text)
        except ET.ParseError:
            continue
        if nested.tag.split("}")[-1] == "retDistDFeInt":
            return nested
        found = nested.find(f".//{{{NFE_NAMESPACE}}}retDistDFeInt")
        if found is not None:
            return found

    fault = root.find(f".//{{{SOAP12_NAMESPACE}}}Fault")
    if fault is not None:
        reason = " ".join(value.strip() for value in fault.itertext() if value.strip())
        raise ValueError(f"Falha SOAP retornada pela SEFAZ: {reason[:500]}")
    raise ValueError("Retorno inesperado da SEFAZ: retDistDFeInt não encontrado")


def _post_query(
    *,
    url: str,
    envelope: str,
    p12_path: str,
    password: str,
) -> bytes:
    certificate_file, key_file = _temporary_pem_files(p12_path, password)
    try:
        headers = {
            "Content-Type": (
                'application/soap+xml; charset=utf-8; '
                f'action="{WSDL_NAMESPACE}/nfeDistDFeInteresse"'
            ),
            "Accept": "application/soap+xml, application/xml, text/xml",
        }
        with httpx.Client(
            cert=(certificate_file, key_file),
            timeout=httpx.Timeout(90.0, connect=30.0),
            follow_redirects=True,
        ) as client:
            response = client.post(url, content=envelope.encode("utf-8"), headers=headers)
            response.raise_for_status()
            return response.content
    except httpx.HTTPError as exc:
        raise ValueError(f"Falha HTTP ao consultar a SEFAZ: {exc}") from exc
    finally:
        Path(certificate_file).unlink(missing_ok=True)
        Path(key_file).unlink(missing_ok=True)


def _parse_result(response_content: bytes, fallback_nsu: str) -> dict:
    result = _locate_distribution_result(response_content)

    def text(tag: str, default: str = "") -> str:
        node = result.find(f"{{{NFE_NAMESPACE}}}{tag}")
        return (node.text or default).strip() if node is not None else default

    documents: list[dict] = []
    batch = result.find(f"{{{NFE_NAMESPACE}}}loteDistDFeInt")
    if batch is not None:
        for zipped in batch.findall(f"{{{NFE_NAMESPACE}}}docZip"):
            encoded = (zipped.text or "").strip()
            if not encoded:
                continue
            try:
                raw_xml = gzip.decompress(base64.b64decode(encoded, validate=True))
            except Exception as exc:
                raise ValueError(
                    f"Não foi possível descompactar o docZip NSU {zipped.attrib.get('NSU', '')}: {exc}"
                ) from exc
            documents.append(
                {
                    "nsu": zipped.attrib.get("NSU", ""),
                    "schema": zipped.attrib.get("schema", ""),
                    "xml": raw_xml,
                }
            )

    status_code = text("cStat")
    status_message = text("xMotivo")
    if not status_code:
        raise ValueError("A SEFAZ não retornou o código de situação")

    return {
        "status_code": status_code,
        "status_message": status_message,
        "last_nsu": text("ultNSU", fallback_nsu).zfill(15),
        "max_nsu": text("maxNSU", fallback_nsu).zfill(15),
        "documents": documents,
    }


def query_distribution(
    cnpj: str,
    uf_code: int,
    last_nsu: str,
    environment: str,
    p12_path: str,
    password: str,
) -> dict:
    normalized_nsu = str(last_nsu or "0").zfill(15)
    url, envelope = _build_query(
        cnpj=cnpj,
        uf_code=uf_code,
        environment=environment,
        mode="distNSU",
        last_nsu=normalized_nsu,
    )
    response_content = _post_query(
        url=url,
        envelope=envelope,
        p12_path=p12_path,
        password=password,
    )
    return _parse_result(response_content, normalized_nsu)


def query_by_access_key(
    cnpj: str,
    uf_code: int,
    access_key: str,
    environment: str,
    p12_path: str,
    password: str,
) -> dict:
    url, envelope = _build_query(
        cnpj=cnpj,
        uf_code=uf_code,
        environment=environment,
        mode="consChNFe",
        access_key=access_key,
    )
    response_content = _post_query(
        url=url,
        envelope=envelope,
        p12_path=p12_path,
        password=password,
    )
    return _parse_result(response_content, "000000000000000")


def summarize_document(raw: bytes) -> dict:
    root = ET.fromstring(raw)
    tag = root.tag.split("}")[-1]
    output = {
        "document_type": tag.upper(),
        "access_key": "",
        "issuer_name": "",
        "issuer_document": "",
        "issue_date": None,
        "total_value": Decimal("0"),
    }
    if tag == "resNFe":
        output["access_key"] = root.findtext("nfe:chNFe", "", NFE_NS)
        output["issuer_name"] = root.findtext("nfe:xNome", "", NFE_NS)
        output["issuer_document"] = root.findtext("nfe:CNPJ", "", NFE_NS)
        date_value = root.findtext("nfe:dhEmi", "", NFE_NS)
        total_value = root.findtext("nfe:vNF", "0", NFE_NS)
    else:
        info = root.find(".//nfe:infNFe", NFE_NS)
        output["access_key"] = (
            ((info.attrib.get("Id") if info is not None else "") or "").replace("NFe", "")
        )
        output["issuer_name"] = root.findtext(".//nfe:emit/nfe:xNome", "", NFE_NS)
        output["issuer_document"] = root.findtext(".//nfe:emit/nfe:CNPJ", "", NFE_NS)
        date_value = root.findtext(".//nfe:ide/nfe:dhEmi", "", NFE_NS)
        total_value = root.findtext(".//nfe:total/nfe:ICMSTot/nfe:vNF", "0", NFE_NS)
    try:
        output["issue_date"] = datetime.fromisoformat(
            (date_value or "").replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except (TypeError, ValueError):
        pass
    try:
        output["total_value"] = Decimal((total_value or "0").replace(",", "."))
    except (TypeError, ValueError):
        pass
    return output
