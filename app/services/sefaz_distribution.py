from __future__ import annotations
import base64, gzip, re, tempfile, xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12
PRODUCTION_URL='https://www1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx'
HOMOLOGATION_URL='https://hom.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx'
NFE_NS={'nfe':'http://www.portalfiscal.inf.br/nfe'}
@dataclass
class CertificateInfo:
    subject:str; issuer:str; serial_number:str; valid_from:datetime; valid_until:datetime
def load_certificate_info(path,password):
    key,cert,_=pkcs12.load_key_and_certificates(Path(path).read_bytes(),password.encode() if password else None)
    if not key or not cert: raise ValueError('Certificado A1 inválido')
    return CertificateInfo(cert.subject.rfc4514_string(),cert.issuer.rfc4514_string(),str(cert.serial_number),cert.not_valid_before_utc.replace(tzinfo=None),cert.not_valid_after_utc.replace(tzinfo=None))
def _pem(path,password):
    key,cert,chain=pkcs12.load_key_and_certificates(Path(path).read_bytes(),password.encode() if password else None)
    if not key or not cert: raise ValueError('Certificado A1 inválido')
    cp=cert.public_bytes(serialization.Encoding.PEM)+b''.join(x.public_bytes(serialization.Encoding.PEM) for x in (chain or []))
    kp=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
    cf=tempfile.NamedTemporaryFile(delete=False,suffix='.pem'); kf=tempfile.NamedTemporaryFile(delete=False,suffix='.key'); cf.write(cp); kf.write(kp); cf.close(); kf.close(); return cf.name,kf.name
def query_distribution(cnpj,uf_code,last_nsu,environment,p12_path,password):
    cnpj=re.sub(r'\D','',cnpj or '')
    if len(cnpj)!=14: raise ValueError('CNPJ inválido')
    nsu=str(last_nsu or '0').zfill(15); amb='1' if environment.upper()=='PRODUCAO' else '2'; url=PRODUCTION_URL if amb=='1' else HOMOLOGATION_URL
    dist=f'<distDFeInt xmlns="http://www.portalfiscal.inf.br/nfe" versao="1.01"><tpAmb>{amb}</tpAmb><cUFAutor>{uf_code}</cUFAutor><CNPJ>{cnpj}</CNPJ><distNSU><ultNSU>{nsu}</ultNSU></distNSU></distDFeInt>'
    env=f'<?xml version="1.0" encoding="utf-8"?><soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope"><soap12:Body><nfeDistDFeInteresse xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe"><nfeDadosMsg>{dist}</nfeDadosMsg></nfeDistDFeInteresse></soap12:Body></soap12:Envelope>'
    cf,kf=_pem(p12_path,password)
    try:
        with httpx.Client(cert=(cf,kf),timeout=60) as client: resp=client.post(url,content=env.encode(),headers={'Content-Type':'application/soap+xml; charset=utf-8'}); resp.raise_for_status()
    finally: Path(cf).unlink(missing_ok=True); Path(kf).unlink(missing_ok=True)
    root=ET.fromstring(resp.content); ret=root.find('.//{http://www.portalfiscal.inf.br/nfe}retDistDFeInt')
    if ret is None: raise ValueError('Retorno inesperado da SEFAZ')
    def t(tag,default=''):
        n=ret.find('{http://www.portalfiscal.inf.br/nfe}'+tag); return (n.text or default).strip() if n is not None else default
    docs=[]; lote=ret.find('{http://www.portalfiscal.inf.br/nfe}loteDistDFeInt')
    if lote is not None:
        for x in lote.findall('{http://www.portalfiscal.inf.br/nfe}docZip'): docs.append({'nsu':x.attrib.get('NSU',''),'schema':x.attrib.get('schema',''),'xml':gzip.decompress(base64.b64decode(x.text or ''))})
    return {'status_code':t('cStat'),'status_message':t('xMotivo'),'last_nsu':t('ultNSU',nsu),'max_nsu':t('maxNSU',nsu),'documents':docs}
def summarize_document(raw):
    root=ET.fromstring(raw); tag=root.tag.split('}')[-1]; out={'document_type':tag.upper(),'access_key':'','issuer_name':'','issuer_document':'','issue_date':None,'total_value':Decimal('0')}
    if tag=='resNFe':
        out['access_key']=root.findtext('nfe:chNFe','',NFE_NS); out['issuer_name']=root.findtext('nfe:xNome','',NFE_NS); out['issuer_document']=root.findtext('nfe:CNPJ','',NFE_NS); dt=root.findtext('nfe:dhEmi','',NFE_NS); val=root.findtext('nfe:vNF','0',NFE_NS)
    else:
        inf=root.find('.//nfe:infNFe',NFE_NS); out['access_key']=((inf.attrib.get('Id') if inf is not None else '') or '').replace('NFe',''); out['issuer_name']=root.findtext('.//nfe:emit/nfe:xNome','',NFE_NS); out['issuer_document']=root.findtext('.//nfe:emit/nfe:CNPJ','',NFE_NS); dt=root.findtext('.//nfe:ide/nfe:dhEmi','',NFE_NS); val=root.findtext('.//nfe:total/nfe:ICMSTot/nfe:vNF','0',NFE_NS)
    try: out['issue_date']=datetime.fromisoformat((dt or '').replace('Z','+00:00')).replace(tzinfo=None)
    except: pass
    try: out['total_value']=Decimal((val or '0').replace(',','.'))
    except: pass
    return out
