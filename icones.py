"""
Ícones desenhados com numpy, sem arquivos de imagem.

Cada ícone é descrito como linhas/círculos em coordenadas 0..1, renderizado
com antisserrilhamento (supersampling) e convertido em PNG para o tkinter.
Assim os ícones ficam nítidos e podem ser recoloridos para o tema claro/escuro.
"""

import base64
import struct
import zlib

import numpy as np

SS = 4  # supersampling


# ---------------------------------------------------------------- PNG

def _png(rgba: np.ndarray) -> bytes:
    h, w, _ = rgba.shape
    bruto = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def bloco(tipo, dados):
        return (struct.pack(">I", len(dados)) + tipo + dados
                + struct.pack(">I", zlib.crc32(tipo + dados) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + bloco(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + bloco(b"IDAT", zlib.compress(bruto, 9))
            + bloco(b"IEND", b""))


def para_photoimage(rgba: np.ndarray, mestre=None):
    import tkinter as tk
    return tk.PhotoImage(master=mestre, data=base64.b64encode(_png(rgba)).decode("ascii"))


def _rgb(hexa: str):
    hexa = hexa.lstrip("#")
    return np.array([int(hexa[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float64)


# ---------------------------------------------------------------- desenho

class _Tela:
    """Grade supersampled; camadas compostas com 'over'. Distâncias em 0..1."""

    def __init__(self, tamanho: int):
        self.tamanho = tamanho
        self.n = tamanho * SS
        c = (np.arange(self.n) + 0.5) / self.n
        self.x, self.y = np.meshgrid(c, c)
        self.cor = np.zeros((self.n, self.n, 3))
        self.alfa = np.zeros((self.n, self.n))

    def pintar(self, distancia, cor: str, opacidade=1.0):
        cobertura = np.clip(0.5 - distancia * self.n / 1.2, 0, 1) * opacidade
        rgb = _rgb(cor)
        self.cor = rgb * cobertura[..., None] + self.cor * (1 - cobertura[..., None])
        self.alfa = cobertura + self.alfa * (1 - cobertura)

    def resultado(self) -> np.ndarray:
        t, s = self.tamanho, SS
        alfa = self.alfa.reshape(t, s, t, s).mean(axis=(1, 3))
        # self.cor está "pré-multiplicada" pela cobertura acumulada
        cor = self.cor.reshape(t, s, t, s, 3).mean(axis=(1, 3))
        cor = cor / np.maximum(alfa, 1e-9)[..., None]
        return np.dstack([cor, alfa * 255]).round().clip(0, 255).astype(np.uint8)


def _segmento(x, y, a, b):
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    t = np.clip(((x - ax) * dx + (y - ay) * dy) / max(dx * dx + dy * dy, 1e-12), 0, 1)
    return np.hypot(x - (ax + t * dx), y - (ay + t * dy))


def _linhas(x, y, *polilinhas, fechar=False):
    d = np.full(x.shape, np.inf)
    for pts in polilinhas:
        pares = list(zip(pts, pts[1:])) + ([(pts[-1], pts[0])] if fechar else [])
        for a, b in pares:
            d = np.minimum(d, _segmento(x, y, a, b))
    return d


def _arco(cx, cy, r, ang_ini, ang_fim, passos=24):
    ang = np.radians(np.linspace(ang_ini, ang_fim, passos))
    return [(cx + r * np.cos(a), cy + r * np.sin(a)) for a in ang]


def _retangulo(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


# Cada ícone: função (x, y) -> distância até o traço (antes de descontar a espessura)
_ICONES = {
    "copiar": lambda x, y: _linhas(
        x, y, _retangulo(0.36, 0.36, 0.86, 0.86), [(0.14, 0.64), (0.14, 0.14), (0.64, 0.14)]),
    "lixeira": lambda x, y: _linhas(
        x, y, [(0.12, 0.26), (0.88, 0.26)], [(0.38, 0.26), (0.40, 0.12), (0.60, 0.12), (0.62, 0.26)],
        [(0.22, 0.26), (0.28, 0.88), (0.72, 0.88), (0.78, 0.26)],
        [(0.43, 0.42), (0.43, 0.72)], [(0.57, 0.42), (0.57, 0.72)]),
    "mais": lambda x, y: _linhas(x, y, [(0.5, 0.14), (0.5, 0.86)], [(0.14, 0.5), (0.86, 0.5)]),
    "remover": lambda x, y: _linhas(x, y, [(0.22, 0.22), (0.78, 0.78)], [(0.78, 0.22), (0.22, 0.78)]),
    "cima": lambda x, y: _linhas(x, y, [(0.5, 0.86), (0.5, 0.16)], [(0.2, 0.46), (0.5, 0.16), (0.8, 0.46)]),
    "baixo": lambda x, y: _linhas(x, y, [(0.5, 0.14), (0.5, 0.84)], [(0.2, 0.54), (0.5, 0.84), (0.8, 0.54)]),
    "ordenar": lambda x, y: _linhas(
        x, y, [(0.08, 0.22), (0.48, 0.22)], [(0.08, 0.5), (0.38, 0.5)], [(0.08, 0.78), (0.28, 0.78)],
        [(0.74, 0.14), (0.74, 0.86)], [(0.56, 0.68), (0.74, 0.86), (0.92, 0.68)]),
    "salvar": lambda x, y: _linhas(
        x, y, [(0.5, 0.1), (0.5, 0.62)], [(0.28, 0.4), (0.5, 0.62), (0.72, 0.4)],
        [(0.14, 0.64), (0.14, 0.88), (0.86, 0.88), (0.86, 0.64)]),
    "atualizar": lambda x, y: _linhas(
        x, y, _arco(0.5, 0.52, 0.34, -40, 250), [(0.62, 0.20), (0.78, 0.30), (0.80, 0.11)]),
    "transcrever": lambda x, y: _linhas(x, y, [(0.28, 0.14), (0.28, 0.86), (0.86, 0.5)], fechar=True),
    "parar": lambda x, y: _linhas(x, y, _retangulo(0.2, 0.2, 0.8, 0.8)),
    "sol": lambda x, y: np.minimum(
        np.abs(np.hypot(x - 0.5, y - 0.5) - 0.17),
        _linhas(x, y, *[[(0.5 + 0.29 * np.cos(a), 0.5 + 0.29 * np.sin(a)),
                         (0.5 + 0.42 * np.cos(a), 0.5 + 0.42 * np.sin(a))]
                        for a in np.radians(np.arange(0, 360, 45))])),
    # lua crescente: contorno de (círculo grande) menos (círculo deslocado)
    "lua": lambda x, y: np.abs(np.maximum(np.hypot(x - 0.48, y - 0.52) - 0.36,
                                          -(np.hypot(x - 0.70, y - 0.34) - 0.30))),
    "pasta": lambda x, y: _linhas(
        x, y, [(0.1, 0.22), (0.38, 0.22), (0.48, 0.32), (0.9, 0.32), (0.9, 0.84), (0.1, 0.84)],
        fechar=True),
}


def icone(nome: str, tamanho: int, cor: str, espessura=0.085) -> np.ndarray:
    tela = _Tela(tamanho)
    tela.pintar(_ICONES[nome](tela.x, tela.y) - espessura / 2, cor)
    return tela.resultado()


# ---------------------------------------------------------------- botão gravar

def _mic(x, y, cx, cy, escala):
    """Distância até um microfone (cápsula + arco + haste), centrado em (cx, cy)."""
    u, v = (x - cx) / escala + 0.5, (y - cy) / escala + 0.5
    yy = np.clip(v, 0.26, 0.46)
    capsula = np.hypot(u - 0.5, v - yy) - 0.13
    arco = _linhas(u, v, _arco(0.5, 0.44, 0.24, 0, 180), [(0.5, 0.68), (0.5, 0.84)],
                   [(0.36, 0.84), (0.64, 0.84)]) - 0.04
    return np.minimum(capsula, arco) * escala


def icone_app(tamanho: int, gravando=False) -> np.ndarray:
    """Ícone do app: microfone branco em círculo azul (vermelho = gravando)."""
    tela = _Tela(tamanho)
    x, y = tela.x, tela.y
    tela.pintar(np.hypot(x - 0.5, y - 0.5) - 0.48, "#e5484d" if gravando else "#2563eb")
    tela.pintar(_mic(x, y, 0.5, 0.5, 0.66), "#ffffff")
    return tela.resultado()


def botao_gravar(tamanho: int, gravando: bool, cor="#e5484d", pulso=0.0) -> np.ndarray:
    """Botão redondo. pulso (0..1) desenha um anel que se expande e some."""
    tela = _Tela(tamanho)
    x, y = tela.x, tela.y
    raio = 0.34
    dist_centro = np.hypot(x - 0.5, y - 0.5)
    if gravando and pulso > 0:
        r_anel = raio + 0.15 * pulso
        tela.pintar(dist_centro - r_anel, cor, opacidade=0.35 * (1 - pulso))
    tela.pintar(dist_centro - raio, cor)
    if gravando:  # quadrado arredondado = "parar"
        lado, canto = 0.10, 0.03
        qx, qy = np.abs(x - 0.5) - (lado - canto), np.abs(y - 0.5) - (lado - canto)
        d = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - canto
        tela.pintar(d, "#ffffff")
    else:
        tela.pintar(_mic(x, y, 0.5, 0.5, 0.42), "#ffffff")
    return tela.resultado()
