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

ROOT = Path("tse_propostas_2026")
RAW_DIR = ROOT / "_zips_brutos"
EXTRACTED_DIR = ROOT / "_extraido"
LOG_PATH = ROOT / "execucao.log"


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


def load_valid_candidate_ids(candidates_zip: Path) -> set[str]:
    """
    Lê consulta_cand_2026.zip e devolve o conjunto de SQ_CANDIDATO que são
    GOVERNADOR ou PRESIDENTE. Usado para filtrar, dentro do zip de propostas,
    somente os PDFs que pertencem a esses titulares.
    """
    candidate_ids = set()
    with zipfile.ZipFile(candidates_zip) as z:
        csv_names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        for name in csv_names:
            with z.open(name) as f:
                text_stream = io.TextIOWrapper(f, encoding="latin-1", errors="replace")
                reader = csv.DictReader(text_stream, delimiter=";")
                for row in reader:
                    office = (row.get("DS_CARGO") or "").strip().upper()
                    if office in TARGET_OFFICES:
                        candidate_ids.add(row.get("SQ_CANDIDATO"))
    logging.info("cadastro de candidatos: %d candidaturas a governador/presidente", len(candidate_ids))
    return candidate_ids


def extract_candidate_id(filename: str) -> str | None:
    match = PDF_FILENAME_PATTERN.match(Path(filename).stem.upper())
    return match.group(1) if match else None


def extract_matching_pdfs(zip_path: Path, destination: Path, valid_candidate_ids: set[str]) -> list[str]:
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
            if candidate_id not in valid_candidate_ids:
                logging.info("ignorado (não é titular governador/presidente): %s", entry.filename)
                continue
            z.extract(entry, destination)
            extracted_files.append(entry.filename)
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

    ROOT.mkdir(exist_ok=True)
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
    valid_candidate_ids: set[str] = set()
    if download_file(session, CANDIDATES_URL, candidates_zip_path, args.force):
        valid_candidate_ids = load_valid_candidate_ids(candidates_zip_path)
    time.sleep(DOWNLOAD_DELAY_SECONDS)

    for state in states:
        url = PROPOSAL_URL_TEMPLATE.format(uf=state)
        zip_path = RAW_DIR / f"proposta_governo_2026_{state}.zip"

        ok = download_file(session, url, zip_path, args.force)
        time.sleep(DOWNLOAD_DELAY_SECONDS)
        if not ok:
            continue

        extracted_dir = EXTRACTED_DIR / state
        extract_matching_pdfs(zip_path, extracted_dir, valid_candidate_ids)

    print(f"\nConcluído. PDFs extraídos em {EXTRACTED_DIR}")


if __name__ == "__main__":
    main()
