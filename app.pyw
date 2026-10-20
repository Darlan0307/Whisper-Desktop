"""
Transcrição de Áudio — app de desktop 100% local (faster-whisper).

Abas:
  • Ditar: grava do microfone e transcreve.
  • Áudios do WhatsApp: transcreve vários arquivos em ordem ("Áudio 1:", "Áudio 2:"...).

Visual: tema Sun Valley (sv-ttk), estilo Windows 11, claro/escuro.
Abrir sem terminal: dois cliques neste arquivo (.pyw usa o pythonw.exe) ou pelo
atalho criado com:  python instalar_atalho.py
"""

import os

# A biblioteca de cálculo do numpy reserva ~400 MB para usar vários núcleos;
# o app não precisa disso (o Whisper usa as próprias threads). Precisa vir
# antes de qualquer "import numpy".
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import json
import queue
import re
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

PASTA_APP = Path(__file__).resolve().parent
PASTA_DADOS = Path(os.environ.get("APPDATA", Path.home())) / "TranscricaoAudio"
ARQ_CONFIG = PASTA_DADOS / "config.json"
ARQ_LOG = PASTA_DADOS / "app.log"
ARQ_HISTORICO = PASTA_DADOS / "historico.db"
ARQ_ICONE = PASTA_APP / "icone.ico"
APP_ID = "TranscricaoAudio.Local.1"

PASTA_DADOS.mkdir(parents=True, exist_ok=True)

# O pythonw.exe não tem console (sys.stdout/stderr = None). Qualquer print ou
# barra de progresso de biblioteca quebraria o app, então mandamos para um log.
if sys.stdout is None or sys.stderr is None:
    try:  # o log nunca passa de ~2 MB: o anterior vira app.log.old
        if ARQ_LOG.stat().st_size > 2_000_000:
            ARQ_LOG.replace(ARQ_LOG.with_suffix(".log.old"))
    except OSError:
        pass
    _log = open(ARQ_LOG, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log
print(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} app iniciado =====")

sys.path.insert(0, str(PASTA_APP))

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import numpy as np

import historico
import icones
import microfone
import sons
import transcricao

try:
    import sistema  # atalho global, bandeja, colar (só Windows)
except Exception:
    sistema = None
    traceback.print_exc()

try:
    import sv_ttk
except Exception:  # sem o tema, o app funciona com o visual padrão
    sv_ttk = None
    traceback.print_exc()

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    TEM_DND = True
except Exception:  # arrastar e soltar é opcional; o botão "Adicionar" sempre funciona
    TEM_DND = False
    traceback.print_exc()

if sys.platform == "win32":
    import ctypes
    try:  # texto nítido em telas com escala (125%, 150%...)
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:  # ícone próprio na barra de tarefas (em vez do ícone do Python)
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass

TEMAS = {"Automático": "auto", "Claro": "claro", "Escuro": "escuro"}
DISPOSITIVOS = {"GPU": "gpu", "CPU": "cpu"}
LIMITE_GRAVACAO_MIN = 15  # proteção contra uma gravação esquecida ligada
ESC_SEGURAR_S = 0.8  # segurar Esc por esse tempo descarta o ditado em andamento
DICA_GRAVAR = "Clique no botão vermelho ou aperte F9 para gravar"
LIBERAR_MODELO_APOS_S = 120  # janela escondida + sem uso por 2 min = tira o modelo da RAM
LIBERAR_MODELO_JANELA_ABERTA_S = 600  # janela aberta + sem uso por 10 min = idem
RETENCAO = {"Nunca apagar": 0, "Apagar após 7 dias": 7, "Apagar após 30 dias": 30,
            "Apagar após 90 dias": 90}

PALETAS = {
    "claro": dict(janela="#fafafa", campo="#ffffff", texto="#1c1c1c", suave="#616161",
                  borda="#e0e0e0", destaque="#005fb8", destaque_texto="#ffffff",
                  ok="#0f7b0f", erro="#c42b1c", info="#005fb8", desab="#a0a0a0",
                  zona="#f3f3f3", gravar="#e5484d", gravar_hover="#d93a40"),
    "escuro": dict(janela="#1c1c1c", campo="#2b2b2b", texto="#fafafa", suave="#a8a8a8",
                   borda="#3d3d3d", destaque="#57c8ff", destaque_texto="#000000",
                   ok="#6ccb5f", erro="#ff99a4", info="#57c8ff", desab="#6b6b6b",
                   zona="#242424", gravar="#e5484d", gravar_hover="#f06a6e"),
}


def fonte(tamanho=10, peso="normal"):
    familias = set(tkfont.families())
    for familia in ("Segoe UI Variable Text", "Segoe UI"):
        if familia in familias:
            return (familia, tamanho, peso)
    return ("TkDefaultFont", tamanho, peso)


def windows_em_modo_escuro() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


# ---------------------------------------------------------------- config

def carregar_config() -> dict:
    padrao = {"microfone": None, "modelo": transcricao.MODELO_PADRAO, "tema": "auto",
              "copiar_auto": True, "colar_auto": True, "geometria": None, "pasta_audios": None,
              "historico": True, "historico_dias": 0, "aviso_bandeja_mostrado": False,
              "dispositivo": "gpu", "som": True}
    try:
        padrao.update(json.loads(ARQ_CONFIG.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except Exception:
        traceback.print_exc()  # config corrompida: usa o padrão
    if padrao["modelo"] not in transcricao.MODELOS:
        padrao["modelo"] = transcricao.MODELO_PADRAO
    if padrao["tema"] not in TEMAS.values():
        padrao["tema"] = "auto"
    if padrao["dispositivo"] not in DISPOSITIVOS.values():
        padrao["dispositivo"] = "gpu"
    return padrao


def salvar_config(cfg: dict):
    # Grava num arquivo temporário e troca de uma vez: se o PC desligar no meio,
    # o config.json antigo continua inteiro.
    try:
        temporario = ARQ_CONFIG.with_suffix(".tmp")
        temporario.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        temporario.replace(ARQ_CONFIG)
    except Exception:
        traceback.print_exc()


# ---------------------------------------------------------------- componentes

class Rotulo(ttk.Label):
    """Label de status cuja cor acompanha o tema: tipo = suave | ok | erro | info."""

    def __init__(self, pai, app, texto="", tipo="suave", **kw):
        super().__init__(pai, text=texto, **kw)
        self.app, self.tipo = app, tipo
        app.ao_mudar_tema(self._recolorir)

    def mostrar(self, texto, tipo="suave"):
        self.tipo = tipo
        self.config(text=texto, foreground=self.app.paleta[tipo])

    def _recolorir(self, paleta):
        self.config(foreground=paleta[self.tipo])


def caixa_de_texto(pai, app, altura=10):
    """Text editável com barra de rolagem, nas cores do tema. Retorna (frame, text)."""
    quadro = ttk.Frame(pai)
    texto = tk.Text(quadro, wrap="word", undo=True, height=altura, font=fonte(11),
                    relief="flat", borderwidth=0, highlightthickness=1, padx=12, pady=10)
    barra = ttk.Scrollbar(quadro, orient="vertical", command=texto.yview)
    texto.configure(yscrollcommand=barra.set)
    texto.pack(side="left", fill="both", expand=True)
    barra.pack(side="right", fill="y", padx=(4, 0))

    def recolorir(p):
        texto.config(bg=p["campo"], fg=p["texto"], insertbackground=p["texto"],
                     selectbackground=p["destaque"], selectforeground=p["destaque_texto"],
                     highlightbackground=p["borda"], highlightcolor=p["destaque"])
    app.ao_mudar_tema(recolorir)
    return quadro, texto


class BotaoGravar(tk.Label):
    """Botão redondo vermelho com microfone; vira 'parar' e pulsa enquanto grava."""

    QUADROS = 18

    def __init__(self, pai, app, comando):
        super().__init__(pai, borderwidth=0, cursor="hand2", takefocus=1)
        self.app, self.comando = app, comando
        self.gravando = False
        self._hover = False
        self._quadro = 0
        self._imgs = {}
        self.bind("<Button-1>", lambda e: self.comando())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<space>", lambda e: self.comando())
        self.bind("<Return>", lambda e: self.comando())
        app.ao_mudar_tema(self._recolorir)

    def _recolorir(self, p):
        tam = round(84 * self.app.escala)
        # Guarda as imagens antigas até o fim: se fossem descartadas enquanto o
        # Label ainda as exibe, o tkinter recusaria qualquer configure().
        antigas = self._imgs  # noqa: F841
        self._imgs = {
            "parado": icones.para_photoimage(icones.botao_gravar(tam, False, p["gravar"]), self),
            "hover": icones.para_photoimage(icones.botao_gravar(tam, False, p["gravar_hover"]), self),
            "gravando": [icones.para_photoimage(
                icones.botao_gravar(tam, True, p["gravar"], pulso=i / self.QUADROS), self)
                for i in range(self.QUADROS)],
        }
        self._mostrar()
        self.config(bg=p["janela"])

    def _set_hover(self, valor):
        self._hover = valor
        self._mostrar()

    def definir_gravando(self, valor):
        self.gravando = valor
        self._quadro = 0
        self._mostrar()
        if valor:
            self._animar()

    def _animar(self):
        if not self.gravando:
            return
        self._quadro = (self._quadro + 1) % self.QUADROS
        self._mostrar()
        self.after(55, self._animar)

    def _mostrar(self):
        if not self._imgs:
            return
        if self.gravando:
            self.config(image=self._imgs["gravando"][self._quadro])
        else:
            self.config(image=self._imgs["hover" if self._hover else "parado"])


class Onda(tk.Canvas):
    """Medidor de nível em forma de onda (barrinhas que andam da direita p/ esquerda)."""

    def __init__(self, pai, app, altura=44):
        super().__init__(pai, height=round(altura * app.escala), highlightthickness=0, borderwidth=0)
        self.app = app
        self.niveis = deque(maxlen=200)
        self.ativo = False
        self.bind("<Configure>", lambda e: self.desenhar())
        app.ao_mudar_tema(lambda p: (self.config(bg=p["janela"]), self.desenhar()))

    def adicionar(self, nivel):
        self.niveis.append(nivel)
        self.desenhar()

    def limpar(self):
        self.niveis.clear()
        self.desenhar()

    def desenhar(self):
        self.delete("all")
        p = self.app.paleta
        w, h = self.winfo_width(), self.winfo_height()
        e = self.app.escala
        larg, passo = max(2, round(3 * e)), max(4, round(6 * e))
        n = max(1, w // passo)
        dados = list(self.niveis)[-n:]
        dados = [0.0] * (n - len(dados)) + dados
        meio = h / 2
        cor = p["destaque"] if self.ativo else p["borda"]
        for i, v in enumerate(dados):
            meia = max(larg / 2, v * (h / 2 - 2))
            x = i * passo + 1
            self.create_rectangle(x, meio - meia, x + larg, meio + meia, fill=cor, outline="")


class ZonaSoltar(tk.Canvas):
    """Área 'arraste os áudios aqui' mostrada sobre a lista quando ela está vazia."""

    def __init__(self, pai, app, ao_clicar):
        super().__init__(pai, highlightthickness=0, borderwidth=0, cursor="hand2")
        self.app = app
        self.ativa = False  # True enquanto o usuário arrasta arquivos sobre a janela
        self._img = None
        self.bind("<Configure>", lambda e: self.desenhar())
        self.bind("<Button-1>", lambda e: ao_clicar())
        app.ao_mudar_tema(lambda p: self.desenhar())

    def desenhar(self):
        self.delete("all")
        p = self.app.paleta
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20 or h < 20:
            return
        self.config(bg=p["zona"])
        cor = p["destaque"] if self.ativa else p["suave"]
        self.create_rectangle(6, 6, w - 6, h - 6, outline=p["destaque"] if self.ativa else p["borda"],
                              width=1, dash=(6, 4))
        tam = round(40 * self.app.escala)
        self._img = icones.para_photoimage(
            icones.icone("salvar" if self.ativa else "pasta", tam, cor, espessura=0.06), self)
        cy = h / 2
        self.create_image(w / 2, cy - 30 * self.app.escala, image=self._img)
        titulo = "Solte para adicionar" if self.ativa else "Arraste os áudios do WhatsApp para cá"
        self.create_text(w / 2, cy + 12 * self.app.escala, text=titulo, fill=p["texto"],
                         font=fonte(12, "bold"))
        if not self.ativa:
            self.create_text(w / 2, cy + 36 * self.app.escala, fill=p["suave"], font=fonte(10),
                             text="ou clique aqui para escolher os arquivos")


# ---------------------------------------------------------------- aba Ditar

class Ditado:
    """Uma gravação. Ela é cortada em trechos nas pausas da fala, e cada trecho vai
    sendo transcrito enquanto você continua falando: ao parar, só falta o último."""

    def __init__(self, origem, alvo=None):
        self.origem = origem  # "janela" (botão/F9) ou "atalho" (Ctrl+Shift+Espaço)
        self.alvo = alvo      # janela onde colar o texto (só no atalho)
        self.textos = []      # um por trecho, na ordem; preenchidos na thread de fundo
        self.prontos = 0
        self.erro = None
        self.gravando = True
        self.descartado = False
        self.concluido = False


class AbaDitar(ttk.Frame):
    def __init__(self, pai, app):
        super().__init__(pai, padding=(16, 14))
        self.app = app
        self.gravador = microfone.Gravador()
        self.microfones = []
        self.ditado = None  # Ditado sendo gravado agora
        self.pendentes = 0  # ditados já parados, aguardando transcrição
        self._esc_desde = None
        self._proximo_corte = 0.0

        linha = ttk.Frame(self)
        linha.pack(fill="x")
        ttk.Label(linha, text="Microfone").pack(side="left")
        self.combo_mic = ttk.Combobox(linha, state="readonly", width=50)
        self.combo_mic.pack(side="left", padx=8, fill="x", expand=True)
        self.combo_mic.bind("<<ComboboxSelected>>", self._mic_escolhido)
        self.bt_atualizar = app.botao(linha, "", "atualizar", self.atualizar_microfones)
        self.bt_atualizar.pack(side="left")

        linha = ttk.Frame(self)
        linha.pack(fill="x", pady=(14, 4))
        self.bt_gravar = BotaoGravar(linha, app, self.alternar)
        self.bt_gravar.pack(side="left")
        direita = ttk.Frame(linha)
        direita.pack(side="left", fill="both", expand=True, padx=(12, 0))
        topo = ttk.Frame(direita)
        topo.pack(fill="x")
        self.lb_tempo = ttk.Label(topo, text="00:00", font=fonte(20, "bold"))
        self.lb_tempo.pack(side="left")
        self.lb_dica = Rotulo(topo, app, DICA_GRAVAR)
        self.lb_dica.pack(side="left", padx=12, pady=(6, 0))
        self.onda = Onda(direita, app)
        self.onda.pack(fill="x", pady=(4, 0))

        linha = ttk.Frame(self)
        linha.pack(fill="x", pady=(4, 8))
        self.lb_estado = Rotulo(linha, app)
        self.lb_estado.pack(side="left")
        self.ocupado = ttk.Progressbar(linha, mode="indeterminate", length=120)

        quadro, self.texto = caixa_de_texto(self, app, altura=12)
        quadro.pack(fill="both", expand=True)

        linha = ttk.Frame(self)
        linha.pack(fill="x", pady=(10, 0))
        self.var_auto = tk.BooleanVar(value=bool(app.cfg.get("copiar_auto", True)))
        ttk.Checkbutton(linha, text="Copiar automaticamente cada transcrição",
                        style="Switch.TCheckbutton", variable=self.var_auto,
                        command=self._salvar_opcoes).pack(side="left")
        app.botao(linha, "Limpar", "lixeira", self.limpar).pack(side="right")
        app.botao(linha, "Copiar", "copiar", self.copiar).pack(side="right", padx=8)

        linha = ttk.Frame(self)
        linha.pack(fill="x", pady=(8, 0))
        self.var_colar = tk.BooleanVar(value=bool(app.cfg.get("colar_auto", True)))
        ttk.Checkbutton(linha, text="Atalho Ctrl+Shift+Espaço: colar o texto sozinho no "
                        "programa aberto", style="Switch.TCheckbutton",
                        variable=self.var_colar, command=self._salvar_opcoes).pack(side="left")
        self.lb_atalho = Rotulo(linha, app)
        self.lb_atalho.pack(side="left", padx=10)

        linha = ttk.Frame(self)
        linha.pack(fill="x", pady=(8, 0))
        self.var_som = tk.BooleanVar(value=bool(app.cfg.get("som", True)))
        ttk.Checkbutton(linha, text="Tocar um som quando o ditado pelo atalho terminar",
                        style="Switch.TCheckbutton", variable=self.var_som,
                        command=self._salvar_opcoes).pack(side="left")

        self.atualizar_microfones()

    # --- microfones

    def atualizar_microfones(self):
        if self.gravador.gravando:
            return
        try:
            self.microfones, pos_padrao = microfone.listar_microfones()
        except microfone.ErroMicrofone as erro:
            self.microfones, pos_padrao = [], 0
            self.lb_estado.mostrar(str(erro), "erro")
        nomes = [m["nome"] for m in self.microfones]
        self.combo_mic["values"] = nomes
        if not nomes:
            self.combo_mic.set("")
            self.lb_estado.mostrar("Nenhum microfone encontrado. Conecte um e clique em atualizar (↻).",
                                   "erro")
            return
        salvo = self.app.cfg.get("microfone")
        self.combo_mic.current(nomes.index(salvo) if salvo in nomes else pos_padrao)

    def _mic_escolhido(self, _evento=None):
        self.app.cfg["microfone"] = self.combo_mic.get()
        salvar_config(self.app.cfg)

    def _salvar_opcoes(self):
        self.app.cfg["copiar_auto"] = self.var_auto.get()
        self.app.cfg["colar_auto"] = self.var_colar.get()
        self.app.cfg["som"] = self.var_som.get()
        salvar_config(self.app.cfg)

    # --- gravação

    def alternar(self, _evento=None, origem="janela"):
        if self.gravador.gravando:
            self.parar()
        else:
            self.gravar(origem)

    def gravar(self, origem="janela", alvo=None):
        pos = self.combo_mic.current()
        if pos < 0 or pos >= len(self.microfones):
            self.atualizar_microfones()  # talvez o microfone tenha sido ligado agora
            pos = self.combo_mic.current()
        if pos < 0 or pos >= len(self.microfones):
            self.app.avisar_erro(origem, "Sem microfone",
                                 "Nenhum microfone selecionado.\n\n"
                                 "Conecte um microfone e clique no botão de atualizar (↻).")
            return False
        try:
            self.gravador.iniciar(self.microfones[pos]["indice"])
        except microfone.ErroMicrofone as erro:
            self.app.avisar_erro(origem, "Microfone", str(erro))
            return False
        self.ditado = Ditado(origem, alvo)
        self._esc_desde = None
        self._proximo_corte = 0.0
        self.app.marcar_uso()
        if self.app.transcritor.estado == "liberado":
            self.app.transcritor.carregar()  # carrega enquanto você fala
        self._mic_escolhido()
        self.bt_gravar.definir_gravando(True)
        self.combo_mic.config(state="disabled")
        self.onda.ativo = True
        self.onda.limpar()
        self.lb_dica.mostrar("Gravando... clique de novo (ou F9) para parar, "
                             "segure Esc para descartar", "erro")
        self.app.gravacao_mudou(True, origem)
        self._atualizar_medidor()
        return True

    def _atualizar_medidor(self):
        if not self.gravador.gravando:
            return
        agora = time.monotonic()
        if sistema is not None and sistema.esc_apertado():
            self._esc_desde = self._esc_desde or agora
            if agora - self._esc_desde >= ESC_SEGURAR_S:
                self.descartar()
                return
        else:
            self._esc_desde = None
        if self.gravador.duracao >= LIMITE_GRAVACAO_MIN * 60:
            # Proteção: uma gravação esquecida ligada ocuparia o PC à toa.
            origem = self.ditado.origem
            self.parar()
            self.app.avisar_erro(
                origem, "Gravação encerrada",
                f"A gravação foi parada automaticamente ao chegar a {LIMITE_GRAVACAO_MIN} "
                "minutos.\n\nO que foi gravado será transcrito normalmente. "
                "Se precisar de mais, é só gravar de novo.", tipo="info")
            return
        if agora >= self._proximo_corte:
            self._proximo_corte = agora + 0.5
            trecho = self.gravador.retirar_trecho()
            if trecho is not None:
                self._enviar_trecho(self.ditado, trecho)
        seg = int(self.gravador.duracao)
        self.lb_tempo.config(text=f"{seg // 60:02d}:{seg % 60:02d}")
        self.onda.adicionar(self.gravador.nivel)
        self.app.aviso.atualizar(seg, self.gravador.nivel)
        self.after(60, self._atualizar_medidor)

    def _interface_parada(self, ditado):
        self.bt_gravar.definir_gravando(False)
        self.lb_tempo.config(text="00:00")
        self.combo_mic.config(state="readonly")
        self.onda.ativo = False
        self.onda.desenhar()
        self.lb_dica.mostrar(DICA_GRAVAR)
        self.app.gravacao_mudou(False, ditado.origem)
        self.app.marcar_uso()

    def parar(self):
        audio = self.gravador.parar()
        d, self.ditado = self.ditado, None
        d.gravando = False
        self._interface_parada(d)

        duracao = audio.size / transcricao.TAXA_AMOSTRAGEM
        if not d.textos:  # nenhum trecho foi cortado durante a gravação
            if duracao < 0.5:
                self.lb_estado.mostrar("Gravação muito curta. Tente de novo.", "erro")
                self.app.aviso.mostrar_mensagem("Gravação muito curta", "erro")
                self.app.som_do_ditado(d.origem, "erro")
                return
            if not np.any(audio):
                # zeros absolutos = o Windows entregou silêncio (bloqueio ou mudo)
                msg = ("O microfone só gravou silêncio absoluto.\n\n"
                       "Verifique se ele não está no mudo e se a privacidade do Windows "
                       "permite o acesso.\n\n" + microfone.MSG_PRIVACIDADE)
                self.lb_estado.mostrar("Nada gravado.", "erro")
                self.app.avisar_erro(d.origem, "Microfone sem som", msg)
                return
        if duracao >= 0.3:
            self._enviar_trecho(d, audio)

        self.pendentes += 1
        self._mostrar_pendentes()
        if d.origem == "atalho":
            self.app.aviso.mostrar_mensagem("Transcrevendo...", "info", esconder_ms=0)
        self._talvez_concluir(d)  # caso todos os trechos já estejam prontos

    def descartar(self):
        """Para a gravação sem transcrever (Esc segurado). Trechos já na fila são ignorados."""
        if not self.gravador.gravando:
            return
        self.gravador.cancelar()
        d, self.ditado = self.ditado, None
        d.gravando = False
        d.descartado = True
        self._interface_parada(d)
        self.lb_estado.mostrar("Ditado descartado.")
        self.app.aviso.mostrar_mensagem("Ditado descartado", "info", 1500)

    def _enviar_trecho(self, d, audio):
        i = len(d.textos)
        d.textos.append("")
        self.app.transcritor.executar(lambda transcrever: self._tarefa(transcrever, d, i, audio))

    def _mostrar_pendentes(self):
        if self.pendentes == 0:
            self.ocupado.stop()
            self.ocupado.pack_forget()
            return
        if self.app.transcritor.estado != "pronto":
            txt = "Aguardando o modelo terminar de carregar..."
        elif self.pendentes == 1:
            txt = "Transcrevendo..."
        else:
            txt = f"Transcrevendo... ({self.pendentes} gravações na fila)"
        self.lb_estado.mostrar(txt, "info")
        if not self.ocupado.winfo_ismapped():
            self.ocupado.pack(side="left", padx=10)
            self.ocupado.start(12)

    def _tarefa(self, transcrever, d, i, audio):
        """Roda na thread de fundo, um trecho por vez e na ordem."""
        erro = None
        if not d.descartado:
            # O fim do trecho anterior vai junto do vocabulário: dá contexto ao modelo
            # e mantém o mesmo estilo de escrita de um trecho para o outro.
            anterior = d.textos[i - 1][-200:] if i else ""
            prompt = f"{transcricao.VOCABULARIO_DEV} {anterior}".strip()
            try:
                d.textos[i] = transcrever(audio, rapido=True, prompt=prompt,
                                          cancelado=lambda: d.descartado)
            except transcricao.Cancelado:
                pass
            except Exception as e:
                traceback.print_exc()
                erro = str(e)
        self.app.na_interface(lambda: self._trecho_pronto(d, erro))

    def _trecho_pronto(self, d, erro):
        d.prontos += 1
        if erro:
            d.erro = erro
        self._talvez_concluir(d)

    def _talvez_concluir(self, d):
        if d.gravando or d.descartado or d.concluido or d.prontos < len(d.textos):
            return
        d.concluido = True
        self._resultado(" ".join(t for t in d.textos if t).strip(), d.erro, d)

    def _resultado(self, texto, erro, d):
        self.pendentes = max(0, self.pendentes - 1)
        self.app.marcar_uso()
        if not texto:
            if erro:
                self.lb_estado.mostrar(f"Erro ao transcrever: {erro}", "erro")
                self.app.aviso.mostrar_mensagem("Erro ao transcrever", "erro")
            else:
                self.lb_estado.mostrar("Nenhuma fala reconhecida na gravação.", "erro")
                self.app.aviso.mostrar_mensagem("Nenhuma fala reconhecida", "erro")
            self.app.som_do_ditado(d.origem, "erro")
        else:
            atual = self.texto.get("1.0", "end-1c")
            separador = "" if not atual or atual.endswith((" ", "\n")) else " "
            self.texto.insert("end", separador + texto)
            self.texto.see("end")
            self.app.salvar_no_historico("Ditado", texto)
            if d.origem == "atalho":
                self.app.entregar_ditado_do_atalho(texto, d.alvo)
            elif self.var_auto.get():
                self.app.copiar_texto(texto)
                self.lb_estado.mostrar("✓ Pronto! Texto copiado (Ctrl+V para colar).", "ok")
            else:
                self.lb_estado.mostrar("✓ Pronto!", "ok")
            if erro:
                self.lb_estado.mostrar(f"⚠ Parte da gravação não foi transcrita: {erro}", "erro")
        self._mostrar_pendentes()

    # --- botões

    def copiar(self):
        texto = self.texto.get("1.0", "end-1c").strip()
        if texto:
            self.app.copiar_texto(texto)
            self.lb_estado.mostrar("✓ Texto copiado.", "ok")

    def limpar(self):
        self.texto.delete("1.0", "end")
        self.lb_estado.mostrar("")

    def encerrar(self):
        if self.gravador.gravando:
            self.gravador.cancelar()


# ---------------------------------------------------------------- aba WhatsApp

class AbaArquivos(ttk.Frame):
    def __init__(self, pai, app):
        super().__init__(pai, padding=(16, 14))
        self.app = app
        self.caminhos = {}   # iid da lista -> Path
        self._contador = 0
        self._embutidos = []  # widgets dentro da caixa de resultado
        self.rodando = False
        self.cancelar_evento = threading.Event()

        meio = ttk.Frame(self)
        meio.pack(fill="both", expand=True)
        self.caixa_lista = ttk.Frame(meio)
        self.caixa_lista.pack(side="left", fill="both", expand=True)
        self.lista = ttk.Treeview(self.caixa_lista, columns=("n", "arquivo", "status"),
                                  show="headings", height=6, selectmode="extended")
        self.lista.heading("n", text="#")
        self.lista.heading("arquivo", text="Arquivo", anchor="w")
        self.lista.heading("status", text="Situação", anchor="w")
        self.lista.column("n", width=44, stretch=False, anchor="center")
        self.lista.column("arquivo", width=300)
        self.lista.column("status", width=170, stretch=False)
        barra = ttk.Scrollbar(self.caixa_lista, orient="vertical", command=self.lista.yview)
        self.lista.configure(yscrollcommand=barra.set)
        self.lista.pack(side="left", fill="both", expand=True)
        barra.pack(side="left", fill="y", padx=(4, 0))
        self.lista.bind("<Delete>", lambda e: self.remover())
        app.ao_mudar_tema(lambda p: (self.lista.tag_configure("erro", foreground=p["erro"]),
                                     self.lista.tag_configure("ativo", foreground=p["info"])))
        self.zona = ZonaSoltar(self.caixa_lista, app, self.escolher)

        lateral = ttk.Frame(meio)
        lateral.pack(side="left", fill="y", padx=(10, 0))
        self.botoes_edicao = []
        for texto, icone, cmd in [("Adicionar", "mais", self.escolher),
                                  ("Subir", "cima", lambda: self.mover(-1)),
                                  ("Descer", "baixo", lambda: self.mover(1)),
                                  ("Remover", "remover", self.remover),
                                  ("Ordenar por nome", "ordenar", self.ordenar),
                                  ("Limpar lista", "lixeira", self.limpar_lista)]:
            b = app.botao(lateral, texto, icone, cmd, estilo="Lateral.TButton")
            b.pack(fill="x", pady=(0, 6))
            self.botoes_edicao.append(b)

        linha = ttk.Frame(self)
        linha.pack(fill="x", pady=(12, 0))
        self.bt_transcrever = app.botao(linha, "Transcrever", "transcrever", self.transcrever,
                                        destaque=True)
        self.bt_transcrever.pack(side="left")
        self.bt_cancelar = app.botao(linha, "Cancelar", "parar", self.cancelar)
        self.bt_cancelar.config(state="disabled")
        self.bt_cancelar.pack(side="left", padx=8)
        self.progresso = ttk.Progressbar(linha, mode="determinate", maximum=100)
        self.progresso.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.lb_estado = Rotulo(self, app)
        self.lb_estado.pack(fill="x", pady=(6, 8))

        quadro, self.texto = caixa_de_texto(self, app, altura=9)
        quadro.pack(fill="both", expand=True)
        self.texto.tag_configure("cab", font=fonte(11, "bold"), spacing1=2)
        app.ao_mudar_tema(lambda p: self.texto.tag_configure("cab", foreground=p["destaque"]))
        linha = ttk.Frame(self)
        linha.pack(fill="x", pady=(10, 0))
        app.botao(linha, "Salvar .txt", "salvar", self.salvar).pack(side="right")
        app.botao(linha, "Copiar tudo", "copiar", self.copiar).pack(side="right", padx=8)
        self.lb_dica = Rotulo(linha, app, "Dica: o botão ao lado de cada áudio copia só aquele texto.")
        self.lb_dica.pack(side="left")

        self._atualizar_zona()

    # --- zona de soltar

    def _atualizar_zona(self):
        vazia = not self.lista.get_children()
        if vazia or self.zona.ativa:
            self.zona.place(x=0, y=0, relwidth=1, relheight=1)
            tk.Misc.lift(self.zona)  # Canvas.lift() move desenhos, não o widget
            self.zona.desenhar()
        else:
            self.zona.place_forget()

    def arrastando(self, ativo):
        if self.rodando:
            return
        self.zona.ativa = ativo
        self._atualizar_zona()

    # --- lista

    def adicionar(self, caminhos):
        """Adiciona arquivos (ou o conteúdo de pastas). Ignora repetidos e
        formatos não aceitos, avisando quais foram ignorados."""
        if self.rodando:
            messagebox.showinfo("Aguarde", "Espere a transcrição atual terminar "
                                "(ou cancele) para mudar a lista.", parent=self)
            return
        existentes = {p.resolve() for p in self.caminhos.values()}
        ignorados, novos = [], []
        for c in caminhos:
            p = Path(c)
            if p.is_dir():
                itens = transcricao.ordenar_por_nome(
                    [f for f in p.iterdir() if f.is_file() and transcricao.extensao_aceita(f)])
            else:
                itens = [p]
            for f in itens:
                if not transcricao.extensao_aceita(f):
                    ignorados.append(f.name)
                elif f.resolve() not in existentes:
                    existentes.add(f.resolve())
                    novos.append(f)
        for f in novos:
            self._contador += 1
            iid = f"f{self._contador}"
            self.caminhos[iid] = f
            self.lista.insert("", "end", iid=iid, values=("", f.name, "na fila"))
        self._renumerar()
        if novos:
            self.app.cfg["pasta_audios"] = str(novos[-1].parent)
            salvar_config(self.app.cfg)
            self.lb_estado.mostrar(f"{len(novos)} áudio(s) adicionado(s). Confira a ordem e "
                                   "clique em Transcrever.")
        if ignorados:
            aceitos = ", ".join(sorted(transcricao.EXTENSOES))
            messagebox.showwarning(
                "Arquivos ignorados",
                "Estes arquivos não são de um formato aceito e foram ignorados:\n\n"
                + "\n".join(ignorados[:15]) + ("\n..." if len(ignorados) > 15 else "")
                + f"\n\nFormatos aceitos: {aceitos}", parent=self)

    def escolher(self):
        if self.rodando:
            return
        padrao = " ".join(f"*{e}" for e in sorted(transcricao.EXTENSOES))
        arquivos = filedialog.askopenfilenames(
            parent=self, title="Escolha os áudios",
            initialdir=self.app.cfg.get("pasta_audios") or str(Path.home() / "Downloads"),
            filetypes=[("Áudios", padrao), ("Todos os arquivos", "*.*")])
        if arquivos:
            self.adicionar(arquivos)

    def _renumerar(self):
        for i, iid in enumerate(self.lista.get_children(), start=1):
            self.lista.set(iid, "n", i)
        self._atualizar_zona()

    def mover(self, direcao):
        selecionados = list(self.lista.selection())
        if not selecionados:
            return
        todos = list(self.lista.get_children())
        posicoes = sorted(todos.index(i) for i in selecionados)
        if direcao < 0 and posicoes[0] == 0:
            return
        if direcao > 0 and posicoes[-1] == len(todos) - 1:
            return
        ordem = posicoes if direcao < 0 else reversed(posicoes)
        for pos in ordem:
            self.lista.move(todos[pos], "", pos + direcao)
        self.lista.see(selecionados[0])
        self._renumerar()

    def remover(self):
        for iid in self.lista.selection():
            self.lista.delete(iid)
            self.caminhos.pop(iid, None)
        self._renumerar()

    def ordenar(self):
        ordem = sorted(self.lista.get_children(),
                       key=lambda iid: transcricao.chave_natural(self.caminhos[iid]))
        for pos, iid in enumerate(ordem):
            self.lista.move(iid, "", pos)
        self._renumerar()

    def limpar_lista(self):
        self.lista.delete(*self.lista.get_children())
        self.caminhos.clear()
        self.progresso["value"] = 0
        self.lb_estado.mostrar("")
        self._renumerar()

    def _status_item(self, iid, texto, tag=()):
        if self.lista.exists(iid):
            self.lista.set(iid, "status", texto)
            self.lista.item(iid, tags=tag)

    # --- transcrição

    def _travar_edicao(self, travar):
        estado = "disabled" if travar else "normal"
        for b in self.botoes_edicao:
            b.config(state=estado)
        self.bt_transcrever.config(state=estado)
        self.bt_cancelar.config(state="normal" if travar else "disabled")

    def transcrever(self):
        iids = list(self.lista.get_children())
        if not iids:
            messagebox.showinfo("Lista vazia", "Adicione os áudios primeiro: clique em "
                                "\"Adicionar\" ou arraste-os para a janela.", parent=self)
            return
        itens = [(iid, self.caminhos[iid]) for iid in iids]
        for iid in iids:
            self._status_item(iid, "na fila")
        self.rodando = True
        self.cancelar_evento.clear()
        self._travar_edicao(True)
        self.progresso["value"] = 0
        self._limpar_resultado()
        self._resultados = []  # (nome do arquivo, texto) para o histórico
        self.app.marcar_uso()
        if self.app.transcritor.estado != "pronto":
            self.lb_estado.mostrar("Aguardando o modelo terminar de carregar...", "info")
        else:
            self.lb_estado.mostrar("Na fila...", "info")
        self.app.transcritor.executar(lambda transcrever: self._tarefa(transcrever, itens))

    def cancelar(self):
        self.cancelar_evento.set()
        self.bt_cancelar.config(state="disabled")
        self.lb_estado.mostrar("Cancelando...", "info")

    def _tarefa(self, transcrever, itens):
        """Roda na thread de fundo: um arquivo por vez; erro em um não para os outros."""
        ui = self.app.na_interface
        total = len(itens)
        feitos = 0
        try:
            feitos = self._transcrever_itens(transcrever, itens, ui, total)
        finally:
            # Sempre destrava a aba (senão ela ficaria "rodando" para sempre e o
            # modelo nunca sairia da memória).
            cancelado = self.cancelar_evento.is_set()
            ui(lambda: self._fim(feitos, total, cancelado))

    def _transcrever_itens(self, transcrever, itens, ui, total):
        feitos = 0
        for n, (iid, caminho) in enumerate(itens):
            if self.cancelar_evento.is_set():
                break

            def progredir(fracao, iid=iid, n=n):
                ui(lambda: self._progresso(iid, n, total, fracao))

            ui(lambda iid=iid, n=n, c=caminho: (
                self._status_item(iid, "transcrevendo...", ("ativo",)),
                self.lista.see(iid),
                self.lb_estado.mostrar(f"Transcrevendo {n + 1} de {total}: {c.name}", "info")))
            try:
                texto = transcrever(caminho, ao_progredir=progredir,
                                    cancelado=self.cancelar_evento.is_set)
                texto = texto or "[sem fala detectada]"
                status, tag = "✓ concluído", "ok"
            except transcricao.Cancelado:
                break
            except Exception as erro:
                traceback.print_exc()
                try:
                    msg = transcricao.mensagem_erro_arquivo(caminho, erro)
                except Exception:
                    msg = str(erro)
                texto = f"[erro ao transcrever: {msg}]"
                status, tag = "✕ erro", "erro"
            feitos += 1
            ui(lambda iid=iid, n=feitos, c=caminho, t=texto, s=status, g=tag:
               self._arquivo_pronto(iid, n, c.name, t, s, g))
        return feitos

    def _progresso(self, iid, n, total, fracao):
        self._status_item(iid, f"transcrevendo... {int(fracao * 100)}%", ("ativo",))
        self.progresso["value"] = (n + fracao) / total * 100

    # --- resultado em blocos: "Áudio N:" + nome do arquivo + botão copiar

    def _limpar_resultado(self):
        self.texto.delete("1.0", "end")
        for w in self._embutidos:
            if w.winfo_exists():
                w.destroy()
        self._embutidos.clear()
        for marca in self.texto.mark_names():
            if marca.startswith(("cab", "corpo")):
                self.texto.mark_unset(marca)

    def _arquivo_pronto(self, iid, n, nome, texto, status, tag):
        self._status_item(iid, status, (tag,))
        self._resultados.append((nome, texto))
        t = self.texto
        if n > 1:
            t.insert("end", "\n\n")
        t.mark_set(f"cab{n}", "end-1c")
        t.mark_gravity(f"cab{n}", "left")
        t.insert("end", f"Áudio {n}:", "cab")
        # O nome do arquivo e o botão são widgets embutidos: não entram no texto
        # copiado, então "Copiar tudo" continua no formato "Áudio N:\ntexto".
        rotulo = ttk.Label(t, text=f"  {nome}", style="Cartao.TLabel")
        botao = self.app.botao(t, "", "copiar", lambda n=n: self._copiar_um(n),
                               estilo="Cartao.Toolbutton")
        for w in (rotulo, botao):
            t.window_create("end-1c", window=w, align="center", padx=2)
            self._embutidos.append(w)
        t.insert("end", "\n")
        t.mark_set(f"corpo{n}", "end-1c")
        t.mark_gravity(f"corpo{n}", "left")
        t.insert("end", texto)
        t.see("end")

    def _copiar_um(self, n):
        marcas = self.texto.mark_names()
        fim = f"cab{n + 1}" if f"cab{n + 1}" in marcas else "end-1c"
        texto = self.texto.get(f"corpo{n}", fim).strip()
        if texto:
            self.app.copiar_texto(texto)
            self.lb_estado.mostrar(f"✓ Texto do Áudio {n} copiado.", "ok")

    def _fim(self, feitos, total, cancelado):
        self.rodando = False
        self._travar_edicao(False)
        self.app.marcar_uso()
        if self._resultados:
            nomes = [n for n, _ in self._resultados]
            titulo = (f"{len(nomes)} áudio(s): " + ", ".join(nomes[:5])
                      + (" ..." if len(nomes) > 5 else ""))
            self.app.salvar_no_historico(
                "WhatsApp", transcricao.formatar_resultado([t for _, t in self._resultados]), titulo)
        erros = sum(1 for iid in self.lista.get_children()
                    if "erro" in self.lista.item(iid, "tags"))
        if cancelado:
            for iid in self.lista.get_children():
                if not self.lista.item(iid, "tags") or "ativo" in self.lista.item(iid, "tags"):
                    self._status_item(iid, "cancelado")
            txt, tipo = f"Cancelado. {feitos} de {total} áudios transcritos.", "info"
        else:
            self.progresso["value"] = 100
            txt, tipo = f"✓ Pronto! {total} áudio(s) transcrito(s).", "ok"
        if erros:
            txt += f" {erros} com erro (veja a lista)."
            tipo = "erro"
        self.lb_estado.mostrar(txt, tipo)
        self.texto.see("1.0")

    def copiar(self):
        texto = self.texto.get("1.0", "end-1c").strip()
        if texto:
            self.app.copiar_texto(texto)
            self.lb_estado.mostrar("✓ Resultado copiado (Ctrl+V para colar).", "ok")

    def salvar(self):
        texto = self.texto.get("1.0", "end-1c").strip()
        if not texto:
            messagebox.showinfo("Nada para salvar", "Transcreva os áudios primeiro.", parent=self)
            return
        destino = filedialog.asksaveasfilename(
            parent=self, title="Salvar transcrição", defaultextension=".txt",
            initialdir=self.app.cfg.get("pasta_audios") or str(Path.home() / "Documents"),
            initialfile=f"transcricao_{datetime.now():%Y-%m-%d_%H-%M}.txt",
            filetypes=[("Texto", "*.txt")])
        if not destino:
            return
        try:
            Path(destino).write_text(texto + "\n", encoding="utf-8")
        except Exception as erro:
            messagebox.showerror("Erro ao salvar", f"Não foi possível salvar o arquivo:\n{erro}",
                                 parent=self)
            return
        self.lb_estado.mostrar(f"✓ Salvo em: {destino}", "ok")


# ---------------------------------------------------------------- aba Histórico

def _data_amigavel(criado: str) -> str:
    """'2026-10-16 14:32:05' -> 'Hoje 14:32' / 'Ontem 14:32' / '16/10/2026 14:32'."""
    try:
        quando = datetime.strptime(criado, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return criado
    dias = (datetime.now().date() - quando.date()).days
    hora = quando.strftime("%H:%M")
    if dias == 0:
        return f"Hoje {hora}"
    if dias == 1:
        return f"Ontem {hora}"
    return quando.strftime("%d/%m/%Y %H:%M")


class AbaHistorico(ttk.Frame):
    LIMITE = 200  # itens mostrados de uma vez (a busca alcança todos)

    def __init__(self, pai, app):
        super().__init__(pai, padding=(16, 14))
        self.app = app
        self._embutidos = []
        self._agendado = None
        self.desatualizado = True

        linha = ttk.Frame(self)
        linha.pack(fill="x")
        ttk.Label(linha, text="Buscar").pack(side="left")
        self.var_busca = tk.StringVar()
        ttk.Entry(linha, textvariable=self.var_busca).pack(side="left", fill="x", expand=True,
                                                           padx=8)
        self.var_busca.trace_add("write", lambda *_: self._agendar())
        self.combo_tipo = ttk.Combobox(linha, state="readonly", width=12,
                                       values=["Tudo", "Ditados", "WhatsApp"])
        self.combo_tipo.set("Tudo")
        self.combo_tipo.bind("<<ComboboxSelected>>", lambda e: self.atualizar())
        self.combo_tipo.pack(side="left")

        self.lb_estado = Rotulo(self, app)
        self.lb_estado.pack(fill="x", pady=(8, 6))

        quadro, self.texto = caixa_de_texto(self, app, altura=14)
        quadro.pack(fill="both", expand=True)
        self.texto.config(cursor="arrow", state="disabled")
        self.texto.tag_configure("cab", font=fonte(10, "bold"), spacing1=4)
        self.texto.tag_configure("meta", font=fonte(9))
        self.texto.tag_configure("corpo", spacing1=2, lmargin1=2, lmargin2=2)
        self.texto.tag_configure("aviso", font=fonte(10), justify="center", spacing1=30)
        app.ao_mudar_tema(lambda p: (self.texto.tag_configure("cab", foreground=p["destaque"]),
                                     self.texto.tag_configure("meta", foreground=p["suave"]),
                                     self.texto.tag_configure("aviso", foreground=p["suave"])))

        linha = ttk.Frame(self)
        linha.pack(fill="x", pady=(10, 0))
        self.var_salvar = tk.BooleanVar(value=bool(app.cfg.get("historico", True)))
        ttk.Checkbutton(linha, text="Salvar transcrições no histórico",
                        style="Switch.TCheckbutton", variable=self.var_salvar,
                        command=self._trocar_salvar).pack(side="left")
        self.combo_retencao = ttk.Combobox(linha, state="readonly", width=18, values=list(RETENCAO))
        dias = app.cfg.get("historico_dias", 0)
        self.combo_retencao.set(next((k for k, v in RETENCAO.items() if v == dias), "Nunca apagar"))
        self.combo_retencao.bind("<<ComboboxSelected>>", self._trocar_retencao)
        self.combo_retencao.pack(side="left", padx=12)
        app.botao(linha, "Limpar tudo", "lixeira", self.limpar_tudo).pack(side="right")

    # --- dados

    def _agendar(self):
        """Busca enquanto digita, sem refazer a lista a cada tecla."""
        if self._agendado:
            self.after_cancel(self._agendado)
        self._agendado = self.after(250, self.atualizar)

    def atualizar(self):
        self._agendado = None
        self.desatualizado = False
        h = self.app.historico
        t = self.texto
        t.config(state="normal")
        t.delete("1.0", "end")
        for w in self._embutidos:
            if w.winfo_exists():
                w.destroy()
        self._embutidos.clear()
        if h is None:
            t.insert("end", "O histórico não pôde ser aberto (veja o app.log).", "aviso")
            t.config(state="disabled")
            return
        tipo = {"Ditados": "Ditado", "WhatsApp": "WhatsApp"}.get(self.combo_tipo.get())
        busca = self.var_busca.get().strip()
        itens = h.listar(busca, tipo, self.LIMITE + 1)
        total = h.contar()
        if not itens:
            if total == 0:
                msg = ("Nenhuma transcrição salva ainda.\n"
                       "Os ditados e os áudios do WhatsApp aparecem aqui.")
            else:
                msg = "Nada encontrado para essa busca."
            t.insert("end", msg, "aviso")
        for item in itens[:self.LIMITE]:
            self._inserir_item(item)
        if len(itens) > self.LIMITE:
            t.insert("end", f"\nMostrando os {self.LIMITE} mais recentes. "
                            "Use a busca para encontrar os mais antigos.", "aviso")
        t.config(state="disabled")
        t.see("1.0")
        if busca or tipo:
            self.lb_estado.mostrar(f"{min(len(itens), self.LIMITE)} encontrado(s) "
                                   f"de {total} no histórico.")
        else:
            self.lb_estado.mostrar(f"{total} transcrição(ões) no histórico. "
                                   "Tudo fica só neste computador.")

    def _inserir_item(self, item):
        t = self.texto
        t.insert("end", _data_amigavel(item["criado"]), "cab")
        meta = f"   {item['tipo']}"
        if item["titulo"]:
            meta += f" · {item['titulo']}"
        t.insert("end", meta + "  ", "meta")
        for icone, cmd in (("copiar", lambda i=item: self.copiar(i)),
                           ("lixeira", lambda i=item: self.excluir(i))):
            b = self.app.botao(t, "", icone, cmd, estilo="Cartao.Toolbutton")
            t.window_create("end-1c", window=b, align="center", padx=1)
            self._embutidos.append(b)
        t.insert("end", "\n" + item["texto"] + "\n\n", "corpo")

    # --- ações

    def copiar(self, item):
        self.app.copiar_texto(item["texto"])
        self.lb_estado.mostrar(f"✓ Copiado ({_data_amigavel(item['criado'])}). "
                               "Ctrl+V para colar.", "ok")

    def excluir(self, item):
        self.app.historico.excluir(item["id"])
        self.atualizar()
        self.lb_estado.mostrar("Item excluído.", "ok")

    def limpar_tudo(self):
        h = self.app.historico
        if h is None:
            return
        total = h.contar()
        if total == 0:
            self.lb_estado.mostrar("O histórico já está vazio.")
            return
        if not messagebox.askyesno(
                "Limpar histórico",
                f"Apagar todas as {total} transcrições do histórico?\n\n"
                "Isso não pode ser desfeito.", icon="warning", parent=self):
            return
        h.limpar_tudo()
        self.atualizar()
        self.lb_estado.mostrar("✓ Histórico apagado.", "ok")

    def _trocar_salvar(self):
        self.app.cfg["historico"] = self.var_salvar.get()
        salvar_config(self.app.cfg)
        if not self.var_salvar.get():
            self.lb_estado.mostrar("Novas transcrições não serão salvas. O que já está aqui "
                                   "continua até você excluir ou clicar em Limpar tudo.", "info")

    def _trocar_retencao(self, _evento=None):
        dias = RETENCAO[self.combo_retencao.get()]
        self.app.cfg["historico_dias"] = dias
        salvar_config(self.app.cfg)
        apagados = self.app.historico.apagar_antigos(dias) if self.app.historico else 0
        self.atualizar()
        if apagados:
            self.lb_estado.mostrar(f"{apagados} item(ns) com mais de {dias} dias apagado(s).", "ok")


# ---------------------------------------------------------------- aviso flutuante

class AvisoFlutuante(tk.Toplevel):
    """Pílula escura no rodapé da tela durante o ditado pelo atalho global.
    Nunca rouba o foco do programa onde você está digitando."""

    FUNDO = "#202020"
    CORES = {"gravando": "#e5484d", "info": "#57c8ff", "ok": "#6ccb5f", "erro": "#ff99a4"}

    def __init__(self, app):
        super().__init__(app.raiz)
        self.app = app
        self.sessao = False  # True do Ctrl+Shift+Espaço até a mensagem final sumir
        self._esconder_id = None
        self.niveis = deque([0.0] * 14, maxlen=14)
        e = app.escala
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.0)
        self.config(bg=self.FUNDO)
        q = tk.Frame(self, bg=self.FUNDO, padx=round(16 * e), pady=round(9 * e))
        q.pack()
        self.ponto = tk.Label(q, text="●", bg=self.FUNDO, font=fonte(12))
        self.ponto.pack(side="left")
        self.msg = tk.Label(q, bg=self.FUNDO, fg="#ffffff", font=fonte(10, "bold"))
        self.msg.pack(side="left", padx=(6, 0))
        self.tempo = tk.Label(q, bg=self.FUNDO, fg="#bdbdbd", font=fonte(10))
        self.barras = tk.Canvas(q, width=round(70 * e), height=round(20 * e), bg=self.FUNDO,
                                highlightthickness=0)
        self.dica = tk.Label(q, text="Ctrl+Shift+Espaço para parar · segure Esc para descartar",
                             bg=self.FUNDO,
                             fg="#8f8f8f", font=fonte(9))
        self.extras = (self.tempo, self.barras, self.dica)
        self.geometry("+-20000+-20000")
        self.update_idletasks()
        if sistema is not None:
            try:
                sistema.janela_flutuante_sem_foco(self)
            except Exception:
                traceback.print_exc()

    def _mostrar(self):
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        if sistema is not None:
            esq, _, dir_, baixo = sistema.area_de_trabalho(self.app.alvo_hwnd)
        else:
            esq, dir_, baixo = 0, self.winfo_screenwidth(), self.winfo_screenheight()
        x = esq + (dir_ - esq - w) // 2
        y = baixo - h - round(28 * self.app.escala)
        self.geometry(f"+{x}+{y}")
        # Com a janela principal escondida (app na bandeja), a 1ª mudança de posição
        # ficava pendente e o aviso continuava fora da tela; isto a aplica na hora.
        self.update_idletasks()
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.94)

    def esconder(self):
        self.sessao = False
        if self._esconder_id:
            self.after_cancel(self._esconder_id)
            self._esconder_id = None
        self.attributes("-alpha", 0.0)
        self.geometry("+-20000+-20000")

    def mostrar_gravando(self):
        self.sessao = True
        if self._esconder_id:
            self.after_cancel(self._esconder_id)
            self._esconder_id = None
        self.ponto.config(fg=self.CORES["gravando"])
        self.msg.config(text="Gravando")
        self.tempo.config(text="00:00")
        for w in self.extras:
            w.pack(side="left", padx=(10, 0))
        self.niveis.extend([0.0] * 14)
        self._desenhar_barras()
        self._mostrar()

    def atualizar(self, seg, nivel):
        if not self.sessao or not self.dica.winfo_ismapped():
            return
        self.tempo.config(text=f"{seg // 60:02d}:{seg % 60:02d}")
        self.niveis.append(nivel)
        self._desenhar_barras()

    def _desenhar_barras(self):
        c = self.barras
        c.delete("all")
        w, h = int(c["width"]), int(c["height"])
        passo = w / len(self.niveis)
        for i, v in enumerate(self.niveis):
            meia = max(1.5, v * (h / 2 - 1))
            x = i * passo + 1
            c.create_rectangle(x, h / 2 - meia, x + passo * 0.55, h / 2 + meia,
                               fill=self.CORES["gravando"], outline="")

    def mostrar_mensagem(self, texto, tipo="ok", esconder_ms=1800, forcar=False):
        """Só aparece durante um ditado pelo atalho (ou se forcar=True)."""
        if not (self.sessao or forcar):
            return
        self.sessao = True
        if self._esconder_id:
            self.after_cancel(self._esconder_id)
            self._esconder_id = None
        for w in self.extras:
            w.pack_forget()
        self.ponto.config(fg=self.CORES.get(tipo, "#ffffff"))
        self.msg.config(text=texto)
        self._mostrar()
        if esconder_ms:
            self._esconder_id = self.after(esconder_ms, self.esconder)


# ---------------------------------------------------------------- janela

class App:
    def __init__(self, instancia=None, minimizado=False):
        self.cfg = carregar_config()
        self.fila_ui = queue.Queue()
        self._callbacks_tema = []
        self._cache_icones = {}
        self._botoes_icone = []  # (botão, ícone, destaque)
        self.escuro = None
        self.paleta = PALETAS["claro"]
        self.instancia = instancia
        self.atalho = None
        self.bandeja = None
        self.alvo_hwnd = None  # janela onde o cursor estava ao apertar o atalho
        self.ultimo_uso = time.monotonic()

        try:
            self.historico = historico.Historico(ARQ_HISTORICO)
            self.historico.apagar_antigos(self.cfg.get("historico_dias", 0))
        except Exception:
            self.historico = None
            traceback.print_exc()
        try:
            self.sons = sons.Sons(PASTA_DADOS / "sons")
        except Exception:
            self.sons = None
            traceback.print_exc()

        self.raiz = TkinterDnD.Tk() if TEM_DND else tk.Tk()
        self.raiz.withdraw()  # só mostra depois de montar e aplicar o tema
        self.raiz.title("Transcrição de Áudio")
        self.escala = max(1.0, self.raiz.winfo_fpixels("1i") / 96)
        self.raiz.minsize(int(680 * self.escala), int(620 * self.escala))
        self.raiz.geometry(self._geometria_inicial())
        if ARQ_ICONE.exists():
            try:
                self.raiz.iconbitmap(default=str(ARQ_ICONE))
            except Exception:
                pass
        self.raiz.report_callback_exception = self._erro_inesperado
        if sv_ttk is not None:
            sv_ttk.set_theme("dark" if self._quer_escuro() else "light")

        self.abas = ttk.Notebook(self.raiz)
        self.aba_ditar = AbaDitar(self.abas, self)
        self.aba_arquivos = AbaArquivos(self.abas, self)
        self.aba_historico = AbaHistorico(self.abas, self)
        self.abas.add(self.aba_ditar, text="  Ditar  ")
        self.abas.add(self.aba_arquivos, text="  Áudios do WhatsApp  ")
        self.abas.add(self.aba_historico, text="  Histórico  ")
        self.abas.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self.abas.bind("<<NotebookTabChanged>>", self._aba_trocada)

        self.bt_tema = ttk.Button(self.raiz, style="Toolbutton", compound="left",
                                  command=self._alternar_tema, cursor="hand2")
        self.bt_tema.place(relx=1.0, x=-14, y=12, anchor="ne")
        self.ao_mudar_tema(self._atualizar_bt_tema)

        barra = ttk.Frame(self.raiz, padding=(14, 8))
        barra.pack(fill="x")
        self.lb_status = Rotulo(barra, self, "Iniciando...")
        self.lb_status.pack(side="left")
        self.combo_modelo = ttk.Combobox(barra, state="readonly", width=9,
                                         values=transcricao.MODELOS)
        self.combo_modelo.set(self.cfg["modelo"])
        self.combo_modelo.bind("<<ComboboxSelected>>", self._trocar_modelo)
        self.combo_modelo.pack(side="right")
        ttk.Label(barra, text="Modelo").pack(side="right", padx=(16, 6))
        self.combo_dispositivo = ttk.Combobox(barra, state="readonly", width=5,
                                              values=list(DISPOSITIVOS))
        self.combo_dispositivo.set(next(k for k, v in DISPOSITIVOS.items()
                                        if v == self.cfg["dispositivo"]))
        self.combo_dispositivo.bind("<<ComboboxSelected>>", self._trocar_dispositivo)
        self.combo_dispositivo.pack(side="right")
        ttk.Label(barra, text="Usar").pack(side="right", padx=(16, 6))
        self.combo_tema = ttk.Combobox(barra, state="readonly", width=11, values=list(TEMAS))
        self.combo_tema.set(next(k for k, v in TEMAS.items() if v == self.cfg["tema"]))
        self.combo_tema.bind("<<ComboboxSelected>>", self._trocar_tema)
        self.combo_tema.pack(side="right")
        ttk.Label(barra, text="Tema").pack(side="right", padx=(0, 6))

        if TEM_DND:
            self.raiz.drop_target_register(DND_FILES)
            self.raiz.dnd_bind("<<Drop>>", self._soltar)
            self.raiz.dnd_bind("<<DropEnter>>", self._arrastar_entrou)
            self.raiz.dnd_bind("<<DropLeave>>", self._arrastar_saiu)
        self.raiz.bind("<F9>", self.aba_ditar.alternar)
        self.raiz.protocol("WM_DELETE_WINDOW", self.fechar)

        self.aplicar_tema(forcar=True)
        self.aviso = AvisoFlutuante(self)
        if not minimizado:
            self.raiz.deiconify()
            self.raiz.after(10, lambda: self._barra_de_titulo(self.escuro))
        self.raiz.after(3000, self._vigiar_tema_windows)

        self.transcritor = transcricao.Transcritor(
            lambda estado, msg: self.na_interface(lambda: self._status(estado, msg)))
        if minimizado:  # aberto com o Windows: o modelo só carrega quando for usado
            self.transcritor.adiar(self.cfg["modelo"], self.cfg["dispositivo"])
        else:
            self.transcritor.carregar(self.cfg["modelo"], self.cfg["dispositivo"])
        self._iniciar_integracoes()
        self._processar_fila()
        self.raiz.after(30_000, self._verificar_ocioso)

    def _geometria_inicial(self) -> str:
        """Tamanho/posição salvos, desde que válidos e visíveis na tela atual
        (ex.: se um 2º monitor foi desconectado, a janela não abre 'fora da tela')."""
        padrao = f"{int(860 * self.escala)}x{int(740 * self.escala)}"
        m = re.fullmatch(r"(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)", str(self.cfg.get("geometria") or ""))
        if not m:
            return padrao
        larg, alt, x, y = (int(v.replace("+-", "-").lstrip("+")) for v in m.groups())
        try:  # área de TODOS os monitores juntos
            metrica = ctypes.windll.user32.GetSystemMetrics
            x0, y0, tela_l, tela_a = metrica(76), metrica(77), metrica(78), metrica(79)
        except Exception:
            x0, y0 = 0, 0
            tela_l, tela_a = self.raiz.winfo_screenwidth(), self.raiz.winfo_screenheight()
        larg, alt = min(larg, tela_l), min(alt, tela_a)
        if larg < 200 or alt < 200:
            return padrao
        if not (x0 - larg + 100 < x < x0 + tela_l - 100 and y0 - 50 <= y < y0 + tela_a - 100):
            return f"{larg}x{alt}"  # posição inválida: deixa o Windows escolher
        return f"{larg}x{alt}+{x}+{y}"

    # --- tema

    def _quer_escuro(self) -> bool:
        tema = self.cfg["tema"]
        return windows_em_modo_escuro() if tema == "auto" else tema == "escuro"

    def ao_mudar_tema(self, funcao):
        """Registra funcao(paleta), chamada agora (se já houver tema) e a cada troca."""
        self._callbacks_tema.append(funcao)
        if self.escuro is not None:
            funcao(self.paleta)

    def aplicar_tema(self, forcar=False):
        escuro = self._quer_escuro()
        if escuro == self.escuro and not forcar:
            return
        self.escuro = escuro
        self.paleta = p = PALETAS["escuro" if escuro else "claro"]
        if sv_ttk is not None:
            sv_ttk.set_theme("dark" if escuro else "light")
        self.raiz.configure(bg=p["janela"])
        # Estilos próprios (precisam ser refeitos a cada troca de tema do ttk)
        estilo = ttk.Style(self.raiz)
        estilo.configure("Accent.TButton", font=fonte(10, "bold"), padding=(16, 6))
        estilo.configure("Lateral.TButton", anchor="w", padding=(10, 5))
        estilo.configure("Cartao.TLabel", background=p["campo"], foreground=p["suave"],
                         font=fonte(9))
        estilo.configure("Cartao.Toolbutton", background=p["campo"], padding=2)
        estilo.configure("Treeview", rowheight=int(30 * self.escala))
        self._botoes_icone = [b for b in self._botoes_icone if b[0].winfo_exists()]
        for botao, nome, destaque in self._botoes_icone:
            botao.config(image=self.imagem_icone(nome, destaque))
        for funcao in self._callbacks_tema:
            try:
                funcao(p)
            except tk.TclError:
                traceback.print_exc()  # ex.: widget já destruído; não derruba a troca de tema
        if not forcar:
            self._barra_de_titulo(escuro)

    def _barra_de_titulo(self, escuro):
        """Deixa a barra de título do Windows escura/clara junto com o tema."""
        if sys.platform != "win32":
            return
        try:
            self.raiz.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.raiz.winfo_id())
            valor = ctypes.c_int(1 if escuro else 0)
            for atributo in (20, 19):  # 20 = Windows 11 / 10 novo; 19 = Windows 10 antigo
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, atributo, ctypes.byref(valor), ctypes.sizeof(valor)) == 0:
                    break
            # força o Windows a redesenhar a barra de título
            w, h = self.raiz.winfo_width(), self.raiz.winfo_height()
            self.raiz.geometry(f"{w + 1}x{h}")
            self.raiz.after(20, lambda: self.raiz.geometry(f"{w}x{h}"))
        except Exception:
            traceback.print_exc()

    def _trocar_tema(self, _evento=None):
        self.cfg["tema"] = TEMAS[self.combo_tema.get()]
        salvar_config(self.cfg)
        self.aplicar_tema()

    def _alternar_tema(self):
        """Botão sol/lua: fixa o tema oposto ao atual (sai do Automático)."""
        self.cfg["tema"] = "claro" if self.escuro else "escuro"
        self.combo_tema.set(next(k for k, v in TEMAS.items() if v == self.cfg["tema"]))
        salvar_config(self.cfg)
        self.aplicar_tema()

    def _atualizar_bt_tema(self, p):
        icone, texto = ("sol", " Tema claro") if self.escuro else ("lua", " Tema escuro")
        self.bt_tema.config(text=texto, image=self.imagem_icone(icone))

    def _vigiar_tema_windows(self):
        """No modo Automático, acompanha quando o Windows muda de claro para escuro."""
        if self.cfg["tema"] == "auto" and self.janela_visivel():
            self.aplicar_tema()
        self.raiz.after(3000, self._vigiar_tema_windows)

    def imagem_icone(self, nome, destaque=False):
        p = self.paleta
        cor = p["destaque_texto"] if destaque else p["texto"]
        tam = round(16 * self.escala)

        def img(c):
            chave = (nome, c, tam)
            if chave not in self._cache_icones:
                self._cache_icones[chave] = icones.para_photoimage(
                    icones.icone(nome, tam, c), self.raiz)
            return self._cache_icones[chave]
        return (img(cor), "disabled", img(p["desab"]))

    def botao(self, pai, texto, icone, comando, destaque=False, estilo=None):
        """ttk.Button com ícone que acompanha o tema."""
        estilo = estilo or ("Accent.TButton" if destaque else "TButton")
        b = ttk.Button(pai, text=texto, command=comando, style=estilo,
                       compound="left" if texto else "image",
                       image=self.imagem_icone(icone, destaque))
        if texto:
            b.config(text=" " + texto)
        self._botoes_icone.append((b, icone, destaque))
        return b

    # --- comunicação thread de fundo -> janela

    def na_interface(self, funcao):
        """Agenda funcao() para rodar na thread da janela (seguro de qualquer thread)."""
        self.fila_ui.put(funcao)

    def _processar_fila(self):
        # Cada atualização roda isolada: um erro numa delas não pode parar a fila
        # (senão a janela "congelaria" em "Transcrevendo..." para sempre).
        while True:
            try:
                funcao = self.fila_ui.get_nowait()
            except queue.Empty:
                break
            try:
                funcao()
            except Exception:
                traceback.print_exc()
        # Janela escondida: olha a fila com menos frequência (menos despertares de CPU).
        self.raiz.after(50 if self.janela_visivel() else 200, self._processar_fila)

    def _status(self, estado, msg):
        tipos = {"carregando": "info", "pronto": "ok", "erro": "erro", "liberado": "suave"}
        self.lb_status.mostrar("●  " + msg, tipos.get(estado, "suave"))
        self.aba_ditar._mostrar_pendentes()
        if estado == "erro":
            messagebox.showerror("Modelo", msg, parent=self.raiz)

    def _trocar_modelo(self, _evento=None):
        novo = self.combo_modelo.get()
        if novo == self.cfg["modelo"]:
            return
        if novo in ("medium", "large-v3"):
            ok = messagebox.askyesno(
                "Trocar modelo",
                f"O modelo \"{novo}\" é mais preciso, porém bem mais lento no processador "
                "(na GPU a diferença é pequena) e precisa ser baixado da internet na primeira "
                "vez (de 1,5 a 3 GB).\n\nDeseja continuar?", parent=self.raiz)
            if not ok:
                self.combo_modelo.set(self.cfg["modelo"])
                return
        self.cfg["modelo"] = novo
        salvar_config(self.cfg)
        self.transcritor.carregar(novo)

    def _trocar_dispositivo(self, _evento=None):
        novo = DISPOSITIVOS[self.combo_dispositivo.get()]
        if novo == self.cfg["dispositivo"]:
            return
        self.cfg["dispositivo"] = novo
        salvar_config(self.cfg)
        self.transcritor.carregar(dispositivo=novo)

    # --- utilidades

    def copiar_texto(self, texto):
        self.raiz.clipboard_clear()
        self.raiz.clipboard_append(texto)
        self.raiz.update_idletasks()

    def _arrastar_entrou(self, evento):
        self.abas.select(self.aba_arquivos)
        self.aba_arquivos.arrastando(True)
        return evento.action

    def _arrastar_saiu(self, evento):
        self.aba_arquivos.arrastando(False)
        return evento.action

    def _soltar(self, evento):
        caminhos = self.raiz.tk.splitlist(evento.data)
        self.abas.select(self.aba_arquivos)
        self.aba_arquivos.arrastando(False)
        self.aba_arquivos.adicionar(caminhos)
        return evento.action

    def _erro_inesperado(self, tipo, valor, tb):
        traceback.print_exception(tipo, valor, tb)
        messagebox.showerror("Erro inesperado",
                             f"{tipo.__name__}: {valor}\n\nDetalhes no arquivo:\n{ARQ_LOG}",
                             parent=self.raiz)

    # --- atalho global, bandeja e segundo plano

    def _iniciar_integracoes(self):
        if sistema is None:
            self.aba_ditar.lb_atalho.mostrar("(atalho global indisponível neste sistema)", "erro")
            return
        try:
            self.atalho = sistema.AtalhoGlobal(lambda: self.na_interface(self._atalho_apertado))
            if self.atalho.iniciar():
                self.aba_ditar.lb_atalho.mostrar("funciona com o app minimizado")
            else:
                self.atalho = None
                self.aba_ditar.lb_atalho.mostrar(
                    "⚠ Ctrl+Shift+Espaço já é usado por outro programa", "erro")
        except Exception:
            self.atalho = None
            traceback.print_exc()
        try:
            self.bandeja = sistema.Bandeja(
                icones.icone_app(64),
                ao_abrir=lambda: self.na_interface(self.mostrar),
                ao_ditar=lambda: self.na_interface(lambda: self._atalho_apertado(pela_bandeja=True)),
                ao_sair=lambda: self.na_interface(self.sair))
            self.bandeja.iniciar()
        except Exception:
            self.bandeja = None  # sem bandeja, o X fecha o app de verdade
            traceback.print_exc()
        if self.instancia is not None:
            self.instancia.esperar_avisos(lambda: self.na_interface(self.mostrar))

    def janela_visivel(self) -> bool:
        try:
            return self.raiz.state() == "normal"
        except tk.TclError:
            return False

    def marcar_uso(self):
        self.ultimo_uso = time.monotonic()

    def _atalho_apertado(self, pela_bandeja=False):
        d = self.aba_ditar
        if d.gravador.gravando:
            # O texto vai para a janela onde você PAROU de gravar: no meio do ditado
            # você pode ter ido para outra tela e voltado (ou mudado de ideia).
            if not pela_bandeja and d.ditado and d.ditado.origem == "atalho":
                agora = sistema.janela_ativa()
                if agora and not sistema.janela_e_deste_app(agora):
                    d.ditado.alvo = self.alvo_hwnd = agora
            d.parar()
            return
        alvo = None if pela_bandeja else sistema.janela_ativa()
        # Atalho apertado com o próprio app em foco: funciona como o botão Gravar.
        if alvo and sistema.janela_e_deste_app(alvo):
            d.gravar("janela")
            return
        self.alvo_hwnd = alvo
        d.gravar("atalho", alvo)  # gravar() já recarrega o modelo, se preciso, enquanto você fala

    def gravacao_mudou(self, gravando, origem):
        if self.bandeja is not None:
            self.bandeja.trocar_imagem(icones.icone_app(64, gravando=gravando))
        if gravando and origem == "atalho":
            self.aviso.mostrar_gravando()

    def som_do_ditado(self, origem, nome):
        """Som só no ditado pelo atalho: é quando o app está em segundo plano."""
        if origem == "atalho" and self.sons is not None and self.aba_ditar.var_som.get():
            self.sons.tocar(nome)

    def entregar_ditado_do_atalho(self, texto, alvo):
        """Cola o texto na janela onde o ditado foi parado. Só cola se aquela MESMA
        janela ainda estiver em foco; senão apenas copia (para não colar no lugar errado)."""
        d = self.aba_ditar
        pode_colar = (d.var_colar.get() and alvo and sistema.janela_ativa() == alvo
                      and not sistema.janela_e_deste_app(alvo))
        if not pode_colar:
            self.copiar_texto(texto)
            d.lb_estado.mostrar("✓ Pronto! Texto copiado (Ctrl+V para colar).", "ok")
            self.aviso.mostrar_mensagem("Copiado: Ctrl+V para colar", "ok", 2500)
            self.som_do_ditado("atalho", "copiado")
            return
        anterior = None
        if not d.var_auto.get():  # devolve depois o que você tinha copiado antes
            try:
                anterior = self.raiz.clipboard_get()
            except tk.TclError:
                anterior = None  # não era texto (ex.: imagem): não dá para restaurar
        self.copiar_texto(texto)
        self._colar_quando_soltar(alvo, anterior, tentativas=40)

    def _colar_quando_soltar(self, alvo, anterior, tentativas):
        d = self.aba_ditar
        if not sistema.teclas_de_atalho_soltas():
            if tentativas > 0:
                self.raiz.after(50, lambda: self._colar_quando_soltar(alvo, anterior, tentativas - 1))
                return
        elif sistema.janela_ativa() == alvo:
            sistema.simular_ctrl_v()
            d.lb_estado.mostrar("✓ Texto colado no programa aberto.", "ok")
            self.aviso.mostrar_mensagem("Colado", "ok", 1500)
            self.som_do_ditado("atalho", "colado")
            if anterior is not None:
                self.raiz.after(700, lambda: self.copiar_texto(anterior))
            return
        d.lb_estado.mostrar("✓ Pronto! Texto copiado (Ctrl+V para colar).", "ok")
        self.aviso.mostrar_mensagem("Copiado: Ctrl+V para colar", "ok", 2500)
        self.som_do_ditado("atalho", "copiado")

    def avisar_erro(self, origem, titulo, mensagem, tipo="erro"):
        if origem == "atalho" and not self.janela_visivel():
            self.aviso.mostrar_mensagem(titulo, "erro" if tipo == "erro" else "info", 3500,
                                        forcar=True)
            if self.bandeja is not None:
                self.bandeja.notificar(mensagem[:250], titulo)
            return
        {"erro": messagebox.showerror, "info": messagebox.showinfo}.get(
            tipo, messagebox.showwarning)(titulo, mensagem, parent=self.raiz)

    def salvar_no_historico(self, tipo, texto, titulo=""):
        if not self.cfg.get("historico", True) or self.historico is None:
            return
        try:
            self.historico.adicionar(tipo, texto, titulo)
        except Exception:
            traceback.print_exc()
            return
        if self.abas.select() == str(self.aba_historico):
            self.aba_historico.atualizar()
        else:
            self.aba_historico.desatualizado = True

    def _aba_trocada(self, _evento=None):
        if self.abas.select() == str(self.aba_historico) and self.aba_historico.desatualizado:
            self.aba_historico.atualizar()

    def _verificar_ocioso(self):
        """Tira o modelo da memória quando não está sendo usado (volta sozinho, em
        ~1 s, na próxima transcrição): 2 min com a janela escondida/minimizada,
        10 min com a janela aberta."""
        try:
            limite = (LIBERAR_MODELO_APOS_S if not self.janela_visivel()
                      else LIBERAR_MODELO_JANELA_ABERTA_S)
            if (self.transcritor.estado == "pronto"
                    and self.transcritor.ocioso and not self.aba_ditar.gravador.gravando
                    and self.aba_ditar.pendentes == 0 and not self.aba_arquivos.rodando
                    and time.monotonic() - self.ultimo_uso > limite):
                self.transcritor.liberar()
        finally:
            self.raiz.after(30_000, self._verificar_ocioso)

    def mostrar(self):
        self.raiz.deiconify()
        self.raiz.lift()
        self.raiz.attributes("-topmost", True)  # traz para frente de verdade no Windows
        self.raiz.after(150, lambda: self.raiz.attributes("-topmost", False))
        self.raiz.focus_force()

    def fechar(self):
        """X da janela: vai para perto do relógio (se a bandeja estiver disponível)."""
        if self.bandeja is None:
            self.sair()
            return
        self.cfg["geometria"] = self.raiz.geometry()
        self.raiz.withdraw()
        if not self.cfg.get("aviso_bandeja_mostrado"):
            self.bandeja.notificar(
                "Continuo aberto perto do relógio. Ctrl+Shift+Espaço dita em qualquer programa. "
                "Para fechar de vez: botão direito no ícone > Sair.")
            self.cfg["aviso_bandeja_mostrado"] = True
        salvar_config(self.cfg)

    def sair(self):
        """Encerra TUDO: gravação, atalho, ícone, modelo e o próprio processo."""
        if self.aba_arquivos.rodando:
            self.mostrar()
            if not messagebox.askyesno("Sair", "Ainda há áudios sendo transcritos. "
                                       "Sair mesmo assim?", parent=self.raiz):
                return
        def guardar_geometria():
            if self.janela_visivel():
                self.cfg["geometria"] = self.raiz.geometry()
            salvar_config(self.cfg)

        # Cada passo isolado: se um falhar, os outros acontecem mesmo assim.
        passos = (self.aba_ditar.encerrar,          # fecha o microfone, se estiver gravando
                  self.aviso.esconder,
                  guardar_geometria,
                  lambda: self.atalho and self.atalho.parar(),      # devolve o Ctrl+Shift+Espaço
                  lambda: self.bandeja and self.bandeja.parar(),    # tira o ícone do relógio
                  lambda: self.instancia and self.instancia.encerrar(),
                  self.transcritor.encerrar,
                  lambda: self.historico and self.historico.fechar())
        try:
            for passo in passos:
                try:
                    passo()
                except Exception:
                    traceback.print_exc()
        finally:
            # Fecha a janela; ao voltar do mainloop, o processo termina com os._exit
            # (mata também uma transcrição que estivesse no meio).
            self.raiz.destroy()

    def rodar(self):
        self.raiz.mainloop()


def main():
    instancia = None
    if sistema is not None:
        instancia = sistema.InstanciaUnica("TranscricaoAudio")
        if not instancia.primeira:
            instancia.avisar_a_outra()  # já está aberto: só mostra a janela existente
            return
    App(instancia, minimizado="--minimizado" in sys.argv).rodar()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        try:
            messagebox.showerror("Erro ao abrir",
                                 f"O aplicativo não conseguiu abrir.\n\n{traceback.format_exc()}"
                                 f"\n\nLog: {ARQ_LOG}")
        except Exception:
            pass
    finally:
        # Garante que nada continua rodando depois de "Sair".
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(0)
