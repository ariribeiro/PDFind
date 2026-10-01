"""
Indexa PDFs no mesmo pdfs.db usado pela versão Node (indexar.mjs).

Uso:   py indexar.py C:\\caminho\\dos\\pdfs
Requer: py -m pip install pymupdf

O OCR usa o Tesseract embutido no PyMuPDF: não precisa instalar o Tesseract,
só o modelo de português, que o script baixa sozinho na primeira vez para ./tessdata.
"""
import os
import shutil
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from multiprocessing import Manager
from pathlib import Path

PARALELO = 5         # arquivos processados ao mesmo tempo (um processo cada)
MIN_CARACTERES = 20  # página com menos texto que isso vai para OCR
DPI_OCR = 150        # resolução da imagem enviada ao OCR
IDIOMA_OCR = "por"
TESSDATA = Path(__file__).resolve().parent / "tessdata"
URL_MODELO = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/por.traineddata"


# ---------- trabalho de cada processo ----------

def iniciar_processo():
    # Silencia mensagens que o MuPDF/Tesseract escrevem direto no terminal
    # (ex.: "Image too small to scale"), para não bagunçar as barras.
    os.dup2(os.open(os.devnull, os.O_WRONLY), 2)
    import pymupdf
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)


def processar(arquivo, fila, tessdata):
    import pymupdf
    t0 = time.time()
    try:
        with pymupdf.open(arquivo) as doc:
            fila.put(("inicio", arquivo, doc.page_count))
            paginas, n_ocr = [], 0
            for i, page in enumerate(doc, 1):
                texto = page.get_text()
                ocr = False
                # Só faz OCR se a própria página não tiver texto extraível
                if len(texto.strip()) < MIN_CARACTERES:
                    tp = page.get_textpage_ocr(
                        language=IDIOMA_OCR, dpi=DPI_OCR, full=True, tessdata=tessdata
                    )
                    texto = page.get_text(textpage=tp)
                    n_ocr += 1
                    ocr = True
                paginas.append(texto)
                fila.put(("pagina", arquivo, i, ocr))
        return arquivo, paginas, n_ocr, None, time.time() - t0
    except Exception as e:
        return arquivo, None, 0, str(e) or type(e).__name__, time.time() - t0


# ---------- terminal ----------

def fmt_tempo(seg):
    s = round(seg)
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s"


def barra(feito, total, tam=20):
    n = round(feito / total * tam) if total else 0
    return "█" * n + "░" * (tam - n)


def nome_curto(arq, maximo=40):
    b = os.path.basename(arq)
    return b[: maximo - 1] + "…" if len(b) > maximo else b.ljust(maximo)


def cabe(s):
    w = max(40, shutil.get_terminal_size((120, 20)).columns - 1)
    return s[: w - 1] + "…" if len(s) > w else s


class Tela:
    """Linhas de arquivos concluídos sobem; as barras ficam fixas embaixo."""

    def __init__(self, total):
        self.tty = sys.stdout.isatty()
        self.total = total
        self.ativos = {}  # arquivo -> {"feitas", "total", "ocr"}
        self.feitos = self.erros = self.paginas_ocr = self.sem_texto = 0
        self.inicio = time.time()
        self.linhas_vivas = 0

    def desenhar(self, permanente=None):
        if not self.tty:
            if permanente:
                print(permanente, flush=True)
            return
        out = f"\x1b[{self.linhas_vivas}A" if self.linhas_vivas else ""
        out += "\x1b[0J"
        if permanente:
            out += cabe(permanente) + "\n"
        vivas = [
            cabe(
                f"  {nome_curto(arq)} {barra(a['feitas'], a['total'])} "
                f"{a['feitas']}/{a['total'] or '?'} pág."
                + (f" ({a['ocr']} OCR)" if a["ocr"] else "")
            )
            for arq, a in self.ativos.items()
        ]
        vivas.append(cabe(
            f"Total {barra(self.feitos, self.total, 30)} {self.feitos}/{self.total} arquivos"
            f" · {self.paginas_ocr} pág. OCR · {self.erros} erros"
            f" · {fmt_tempo(time.time() - self.inicio)}"
        ))
        out += "\n".join(vivas) + "\n"
        self.linhas_vivas = len(vivas)
        sys.stdout.write(out)
        sys.stdout.flush()

    def mensagem(self, msg):
        tipo, arq = msg[0], msg[1]
        if tipo == "inicio":
            self.ativos[arq] = {"feitas": 0, "total": msg[2], "ocr": 0}
        elif tipo == "pagina" and arq in self.ativos:
            self.ativos[arq]["feitas"] = msg[2]
            if msg[3]:
                self.ativos[arq]["ocr"] += 1
                self.paginas_ocr += 1


# ---------- principal ----------

def garantir_modelo():
    modelo = TESSDATA / f"{IDIOMA_OCR}.traineddata"
    if not modelo.exists():
        print("Baixando o modelo de OCR em português (só na primeira vez)...")
        TESSDATA.mkdir(exist_ok=True)
        tmp = modelo.with_suffix(".tmp")
        try:
            urllib.request.urlretrieve(URL_MODELO, tmp)
            tmp.replace(modelo)
        except Exception as e:
            print(f"Aviso: não foi possível baixar o modelo ({e}). "
                  f"Arquivos que precisarem de OCR vão dar erro.")
    return str(TESSDATA)


def main():
    if len(sys.argv) < 2:
        print("Uso: py indexar.py C:\\caminho\\dos\\pdfs")
        sys.exit(1)
    if os.name == "nt":
        os.system("")  # ativa as sequências de cor/cursor no console do Windows
    sys.stdout.reconfigure(errors="replace")

    pasta = sys.argv[1]
    db = sqlite3.connect("pdfs.db")
    db.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
            path UNINDEXED, page UNINDEXED, text,
            tokenize = 'unicode61 remove_diacritics 2'
        );
        CREATE TABLE IF NOT EXISTS indexados (path TEXT PRIMARY KEY);
    """)

    # Mesmo formato de caminho da versão Node (absoluto), para reaproveitar o banco
    todos = [
        os.path.abspath(os.path.join(raiz, nome))
        for raiz, _, nomes in os.walk(pasta)
        for nome in nomes
        if nome.lower().endswith(".pdf")
    ]
    ja = {r[0] for r in db.execute("SELECT path FROM indexados")}
    pendentes = [a for a in todos if a not in ja]

    print(f"{len(todos)} PDFs encontrados, {len(todos) - len(pendentes)} já indexados, "
          f"{len(pendentes)} a processar ({PARALELO} em paralelo).\n")
    if not pendentes:
        return
    tessdata = garantir_modelo()

    tela = Tela(len(pendentes))
    with Manager() as manager, ProcessPoolExecutor(PARALELO, initializer=iniciar_processo) as ex:
        fila = manager.Queue()
        abertos = {ex.submit(processar, a, fila, tessdata) for a in pendentes}

        while abertos:
            prontos, abertos = wait(abertos, timeout=0.15, return_when=FIRST_COMPLETED)
            while not fila.empty():
                tela.mensagem(fila.get())

            for fut in prontos:
                arquivo, paginas, n_ocr, erro, dur = fut.result()
                tela.ativos.pop(arquivo, None)
                tela.feitos += 1
                nome = os.path.basename(arquivo)
                if erro:
                    tela.erros += 1
                    tela.desenhar(f"✗ {nome}: erro: {erro}")
                    continue
                with db:  # transação
                    db.executemany(
                        "INSERT INTO docs (path, page, text) VALUES (?, ?, ?)",
                        [(arquivo, i, t) for i, t in enumerate(paginas, 1)],
                    )
                    db.execute("INSERT INTO indexados (path) VALUES (?)", (arquivo,))
                vazio = all(not t.strip() for t in paginas)
                if vazio:
                    tela.sem_texto += 1
                tela.desenhar(
                    f"{'⚠' if vazio else '✓'} {nome}: {len(paginas)} pág. "
                    f"({len(paginas) - n_ocr} texto, {n_ocr} OCR) em {fmt_tempo(dur)}"
                    + (" — sem texto mesmo após OCR" if vazio else "")
                )
            tela.desenhar()

    print(f"\nConcluído em {fmt_tempo(time.time() - tela.inicio)}: {len(pendentes)} arquivos, "
          f"{tela.erros} com erro, {tela.paginas_ocr} páginas via OCR, "
          f"{tela.sem_texto} sem texto.")


if __name__ == "__main__":
    main()
