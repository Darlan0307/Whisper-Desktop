"""
Histórico local das transcrições (SQLite, arquivo em %APPDATA%\\TranscricaoAudio).

Nada sai do computador. Ao excluir, o conteúdo é sobrescrito no arquivo
(secure_delete) e "Limpar tudo" ainda compacta o banco (VACUUM), para o texto
apagado não ficar recuperável dentro do arquivo.
"""

import sqlite3
import unicodedata
from datetime import datetime, timedelta


def _normalizar(texto):
    """'Açúcar' -> 'acucar' (busca sem acento e sem diferença de maiúsculas)."""
    if texto is None:
        return ""
    sem_acento = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in sem_acento if not unicodedata.combining(c)).casefold()


class Historico:
    def __init__(self, caminho):
        self.con = sqlite3.connect(str(caminho))
        self.con.create_function("norm", 1, _normalizar, deterministic=True)
        self.con.execute("PRAGMA secure_delete = ON")
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS itens (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                criado TEXT NOT NULL,          -- 'AAAA-MM-DD HH:MM:SS' (hora local)
                tipo   TEXT NOT NULL,          -- 'Ditado' | 'WhatsApp'
                titulo TEXT NOT NULL DEFAULT '',
                texto  TEXT NOT NULL
            )""")
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_criado ON itens(criado)")
        self.con.commit()

    def adicionar(self, tipo, texto, titulo=""):
        texto = (texto or "").strip()
        if not texto:
            return None
        cur = self.con.execute(
            "INSERT INTO itens (criado, tipo, titulo, texto) VALUES (?, ?, ?, ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tipo, titulo, texto))
        self.con.commit()
        return cur.lastrowid

    def listar(self, busca="", tipo=None, limite=200):
        """Itens mais recentes primeiro: lista de dicts."""
        sql = "SELECT id, criado, tipo, titulo, texto FROM itens WHERE 1=1"
        args = []
        if tipo:
            sql += " AND tipo = ?"
            args.append(tipo)
        for palavra in _normalizar(busca).split():  # todas as palavras precisam aparecer
            sql += " AND (instr(norm(texto), ?) > 0 OR instr(norm(titulo), ?) > 0)"
            args += [palavra, palavra]
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limite)
        colunas = ("id", "criado", "tipo", "titulo", "texto")
        return [dict(zip(colunas, linha)) for linha in self.con.execute(sql, args)]

    def contar(self):
        return self.con.execute("SELECT COUNT(*) FROM itens").fetchone()[0]

    def excluir(self, id_item):
        self.con.execute("DELETE FROM itens WHERE id = ?", (id_item,))
        self.con.commit()

    def limpar_tudo(self):
        self.con.execute("DELETE FROM itens")
        self.con.commit()
        self.con.execute("VACUUM")

    def apagar_antigos(self, dias):
        """Apaga itens com mais de `dias` dias. Retorna quantos foram apagados."""
        if not dias:
            return 0
        limite = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
        cur = self.con.execute("DELETE FROM itens WHERE criado < ?", (limite,))
        self.con.commit()
        return cur.rowcount

    def fechar(self):
        try:
            self.con.close()
        except Exception:
            pass
