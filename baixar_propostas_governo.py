"""
Baixa as propostas de governo (planos de governo) de todos os candidatos
a governador e presidente, direto do Portal de Dados Abertos do TSE.

Dependência externa: requests
    pip install requests

Uso:
    python baixar_propostas_governo.py --ufs PE,BR    # só Pernambuco + presidente
    python baixar_propostas_governo.py                # todos os estados + presidente
"""

import argparse
import csv
import io
import logging
import re
import sys
import time
import zipfile
from pathlib import Path
from datetime import datetime, timezone
import requests
from requests.adapters import HTTPAdapter, Retry

# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------

ALL_STATES = [
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS",
    "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC",
    "SE", "SP", "TO",
]
PRESIDENT_UF = "BR"

PROPOSAL_URL_TEMPLATE = "https://cdn.tse.jus.br/estatistica/sead/odsele/proposta_governo/proposta_governo_2026_{uf}.zip"
CANDIDATES_URL = "https://cdn.tse.jus.br/estatistica/sead/odsele/consulta_cand/consulta_cand_2026.zip"

TARGET_OFFICES = {"GOVERNADOR", "PRESIDENTE"}

# Nome real do PDF dentro do zip, confirmado via --discovery + inspeção do
# cadastro de candidatos:  <ANO><UF><SQ_CANDIDATO>_<NN>.pdf
# ex: 2026PE170002540337_01.pdf. O grupo capturado é o SQ_CANDIDATO.
PDF_FILENAME_PATTERN = re.compile(r"^\d{4}[A-Z]{2}(\d+)_\d+$")

USER_AGENT = "led-data-acquisition/1.0"
DOWNLOAD_DELAY_SECONDS = 1.5  # educação com o servidor público

ROOT = Path("data/bronze")
RAW_DIR = ROOT / "tse_propostas_2026" / "_zips_brutos"
EXTRACTED_DIR = ROOT / "_extraido"
LOG_PATH = ROOT / "execucao.log"
METADATA_DIR = ROOT / "metadata" 

def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    retries = Retry(
        total=5,
        backoff_factor=2,  # 2s, 4s, 8s, 16s, 32s
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def download_file(session: requests.Session, url: str, destination: Path, force: bool = False) -> bool:
    """Baixa um arquivo com streaming, pulando se já existir. Retorna True se ok."""
    if destination.exists() and destination.stat().st_size > 0 and not force:
        logging.info("já existe, pulando: %s", destination.name)
        return True

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    try:
        with session.get(url, stream=True, timeout=60) as resp:
            if resp.status_code == 404:
                logging.warning("404 (não existe / sem candidatos?): %s", url)
                return False
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
        tmp.rename(destination)
        logging.info("baixado: %s (%.1f KB)", destination.name, destination.stat().st_size / 1024)
        return True
    except requests.RequestException as e:
        logging.error("falha ao baixar %s: %s", url, e)
        if tmp.exists():
            tmp.unlink()
        return False


def load_valid_candidates_metadata(candidates_zip: Path) -> dict:
    """
    Lê consulta_cand_2026.zip e devolve um dicionário de SQ_CANDIDATO 
    com os metadados necessários para o manifest.
    """
    candidates = {}
    with zipfile.ZipFile(candidates_zip) as z:
        csv_names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        for name in csv_names:
            with z.open(name) as f:
                text_stream = io.TextIOWrapper(f, encoding="latin-1", errors="replace")
                reader = csv.DictReader(text_stream, delimiter=";")
                for row in reader:
                    office = (row.get("DS_CARGO") or "").strip().upper()
                    if office in TARGET_OFFICES:
                        sq = row.get("SQ_CANDIDATO")
                        prefix = "PRES" if office == "PRESIDENTE" else "GOV"
                        doc_id = f"{prefix}_{sq}"
                        #id é formado por um prefixo PRES ou GOV e o número único do candidato gerado pelo tse

                        candidates[sq] = {
                            "document_id": doc_id,
                            "candidate": row.get("NM_URNA_CANDIDATO") or row.get("NM_CANDIDATO"),
                            "party": row.get("SG_PARTIDO"),
                            "office": office,
                            "state": row.get("SG_UF"),
                            "status": "arquivo_vazio",
                            "original_filename": "NULL",
                            "filename": "NULL"
                        }
    logging.info("cadastro de candidatos: %d candidaturas a governador/presidente", len(candidates))
    return candidates


def extract_candidate_id(filename: str) -> str | None:
    match = PDF_FILENAME_PATTERN.match(Path(filename).stem.upper())
    return match.group(1) if match else None


def extract_matching_pdfs(zip_path: Path, destination: Path, candidates_dict: dict, source_url: str) -> list[str]:    
    """
    Extrai só os PDFs cujo SQ_CANDIDATO (embutido no nome do arquivo) está em
    valid_candidate_ids. Isso descarta automaticamente vices, o leiame.pdf
    (não bate com o padrão de nome) e qualquer outro arquivo que não seja
    proposta de um titular a governador/presidente.
    """
    destination.mkdir(parents=True, exist_ok=True)
    extracted_files = []
    with zipfile.ZipFile(zip_path) as z:
        for entry in z.infolist():
            if entry.is_dir() or not entry.filename.lower().endswith(".pdf"):
                continue
            candidate_id = extract_candidate_id(Path(entry.filename).name)
            if candidate_id not in candidates_dict:
                logging.info("ignorado (não é titular governador/presidente): %s", entry.filename)
                continue
            
            cand = candidates_dict[candidate_id]
            novo_nome_pdf = f"{cand['document_id']}.pdf"
            
            with z.open(entry) as source, open(destination / novo_nome_pdf, "wb") as target:
                target.write(source.read())
            
            # Atualiza o manifest
            cand["status"] = "ok"
            cand["original_filename"] = entry.filename
            cand["filename"] = novo_nome_pdf
            cand["source_url"] = source_url
            
            extracted_files.append(novo_nome_pdf)
    return extracted_files


# --------------------------------------------------------------------------
# Orquestração
# --------------------------------------------------------------------------

def main() -> None:
    """Baixa o cadastro de candidatos e, em seguida, a proposta de governo de cada UF pedida."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ufs", help="lista separada por vírgula (ex: PE,SP,BR). Padrão: todas + BR")
    parser.add_argument("--force", action="store_true", help="rebaixa mesmo se o arquivo já existir")
    args = parser.parse_args()

    ROOT.mkdir(parents=True, exist_ok=True)
    METADATA_DIR.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )

    session = build_session()

    if args.ufs:
        states = [u.strip().upper() for u in args.ufs.split(",")]
    else:
        states = ALL_STATES + [PRESIDENT_UF]

    candidates_zip_path = RAW_DIR / "consulta_cand_2026.zip"
    candidates_dict = {}
    if download_file(session, CANDIDATES_URL, candidates_zip_path, args.force):
        candidates_dict = load_valid_candidates_metadata(candidates_zip_path)
    time.sleep(DOWNLOAD_DELAY_SECONDS)

    for state in states:
        url = PROPOSAL_URL_TEMPLATE.format(uf=state)
        zip_path = RAW_DIR / f"proposta_governo_2026_{state}.zip"

        ok = download_file(session, url, zip_path, args.force)
        time.sleep(DOWNLOAD_DELAY_SECONDS)
        if not ok:
            for cand in candidates_dict.values():
                if cand["state"] == state:
                    cand["status"] = "erro_download"
                    cand["source_url"] = url
            continue

        extracted_dir = EXTRACTED_DIR / state
        extract_matching_pdfs(zip_path, extracted_dir, candidates_dict, url)

    # geração final do Manifest
    manifest_fields = [
        "document_id", "candidate", "party", "office", "state", "source_url",
        "original_filename", "filename", "download_timestamp", "dataset_version", "status"
    ]
    
    agora_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_path = METADATA_DIR / "documents.csv"
    
    with open(manifest_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=manifest_fields)
        writer.writeheader()
        
        for cand in sorted(candidates_dict.values(), key=lambda x: x["document_id"]):
            if cand["state"] in states:
                
                if "source_url" not in cand:
                    cand["source_url"] = PROPOSAL_URL_TEMPLATE.format(uf=cand["state"])
                
                writer.writerow({
                    "document_id": cand["document_id"],
                    "candidate": cand["candidate"],
                    "party": cand["party"],
                    "office": cand["office"],
                    "state": cand["state"],
                    "source_url": cand["source_url"],
                    "original_filename": cand["original_filename"],
                    "filename": cand["filename"],
                    "download_timestamp": agora_utc,
                    "dataset_version": "bronze_v0",
                    "status": cand["status"]
                })

    print(f"\nConcluído. PDFs extraídos em {EXTRACTED_DIR}")
    print(f"Manifest criado em {manifest_path}")


if __name__ == "__main__":
    main()
