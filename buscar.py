"""
Uso:
  py buscar.py "termo"                        -> mostra no terminal (até 50 resultados)
  py buscar.py "termo" --csv resultados.csv   -> salva TODOS os resultados em CSV
"""
import csv
import os
import re
import sqlite3
import sys

args = sys.argv[1:]
arquivo_csv = None
if "--csv" in args:
    i = args.index("--csv")
    arquivo_csv = args[i + 1] if i + 1 < len(args) else "resultados.csv"
    del args[i:i + 2]
termo = " ".join(args)
if not termo:
    print('Uso: py buscar.py "termo" [--csv resultados.csv]')
    sys.exit(1)

sys.stdout.reconfigure(errors="replace")
db = sqlite3.connect("pdfs.db")
rows = db.execute(f"""
    SELECT path, page, snippet(docs, 2, '[', ']', '…', 20)
    FROM docs WHERE docs MATCH ? ORDER BY rank
    {"" if arquivo_csv else "LIMIT 50"}
""", (termo,)).fetchall()

limpa = lambda t: re.sub(r"\s+", " ", str(t)).strip()

if arquivo_csv:
    # ";" e BOM UTF-8 para o Excel em português abrir com colunas e acentos certos
    with open(arquivo_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";", quoting=csv.QUOTE_NONNUMERIC)
        w.writerow(["arquivo", "pagina", "trecho", "caminho"])
        for path, page, trecho in rows:
            w.writerow([os.path.basename(path), int(page), limpa(trecho), path])
    print(f"{len(rows)} resultado(s) salvos em {arquivo_csv}")
else:
    for path, page, trecho in rows:
        print(f"{path} (p. {page})\n  {limpa(trecho)}\n")
    print(f"{len(rows)} resultado(s)")
