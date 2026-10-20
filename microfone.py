"""
Microfone: lista os dispositivos de entrada (sem repetições) e grava na memória
em 16 kHz, mono, float32, pronto para entregar ao Whisper. Não cria arquivo.

O Windows lista o mesmo aparelho uma vez por "host API" (MME, DirectSound,
WASAPI, WDM-KS), e no MME os nomes ainda vêm cortados em 31 letras. Por isso
usamos só a lista do WASAPI (nomes completos, um por aparelho) e, se ela não
existir, a do MME, removendo nomes repetidos.
"""

import threading
import time

import numpy as np

TAXA = 16000  # taxa que o Whisper espera

MSG_PRIVACIDADE = (
    "O Windows está bloqueando o acesso ao microfone.\n\n"
    "Abra Configurações > Privacidade e segurança > Microfone e ative:\n"
    "  • \"Acesso ao microfone\"\n"
    "  • \"Permitir que aplicativos acessem o microfone\"\n"
    "  • \"Permitir que aplicativos da área de trabalho acessem o microfone\"\n\n"
    "Depois feche e abra este aplicativo de novo."
)


class ErroMicrofone(Exception):
    """Erro com mensagem já em português, pronta para mostrar ao usuário."""


def _sd():
    try:
        import sounddevice as sd
        return sd
    except Exception as erro:  # PortAudio ausente etc.
        raise ErroMicrofone(f"Não foi possível acessar o sistema de áudio: {erro}") from erro


def privacidade_bloqueada() -> bool:
    """True se as configurações de privacidade do Windows negam o microfone
    para aplicativos de área de trabalho (como este)."""
    try:
        import winreg
    except ImportError:
        return False
    base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    chaves = [
        (winreg.HKEY_LOCAL_MACHINE, base),             # "Acesso ao microfone" (geral)
        (winreg.HKEY_CURRENT_USER, base),              # todos os apps
        (winreg.HKEY_CURRENT_USER, base + r"\NonPackaged"),  # apps da área de trabalho
    ]
    for raiz, caminho in chaves:
        try:
            with winreg.OpenKey(raiz, caminho) as k:
                valor, _ = winreg.QueryValueEx(k, "Value")
                if str(valor).lower() == "deny":
                    return True
        except OSError:
            continue
    return False


def listar_microfones():
    """Retorna (lista, posicao_padrao). Cada item: {"nome", "indice", "api"}."""
    sd = _sd()
    try:
        apis = sd.query_hostapis()
        dispositivos = sd.query_devices()
    except Exception as erro:
        raise ErroMicrofone(f"Não foi possível listar os microfones: {erro}") from erro

    def da_api(nome_api):
        for api in apis:
            if api["name"] == nome_api:
                return api
        return None

    api = da_api("Windows WASAPI") or da_api("MME")
    if api is not None:
        indices = api["devices"]
        padrao = api["default_input_device"]
        nome_api = api["name"]
    else:  # fora do Windows: usa tudo
        indices = range(len(dispositivos))
        padrao = sd.default.device[0]
        nome_api = ""

    lista, vistos, pos_padrao = [], set(), 0
    for i in indices:
        d = dispositivos[i]
        if d["max_input_channels"] < 1:
            continue
        nome = d["name"].strip()
        if nome.lower() in vistos:
            continue
        vistos.add(nome.lower())
        if i == padrao:
            pos_padrao = len(lista)
        lista.append({"nome": nome, "indice": i, "api": nome_api})
    return lista, pos_padrao


def _traduzir_erro(erro: Exception) -> str:
    if privacidade_bloqueada():
        return MSG_PRIVACIDADE
    texto = str(erro)
    if "-9996" in texto or "Invalid device" in texto:
        return "Esse microfone não foi encontrado. Ele foi desconectado? Clique em \"Atualizar\" na lista."
    if "-9985" in texto or "unavailable" in texto.lower():
        return ("O microfone está ocupado ou indisponível. Feche outros programas que estejam "
                "usando o microfone (Teams, Zoom, Discord...) e tente de novo.")
    if "-9999" in texto or "Unanticipated host error" in texto:
        return ("O Windows recusou abrir o microfone. Isso costuma ser a privacidade do Windows.\n\n"
                + MSG_PRIVACIDADE + f"\n\n(detalhe técnico: {texto})")
    return f"Não foi possível usar o microfone: {texto}"


def _reamostrar(audio: np.ndarray, taxa_orig: int) -> np.ndarray:
    """Converte para 16 kHz. Faz uma média móvel (filtro passa-baixa simples,
    evita chiado de aliasing) e depois interpolação linear."""
    if taxa_orig == TAXA or audio.size == 0:
        return audio.astype(np.float32)
    fator = taxa_orig / TAXA
    janela = int(round(fator))
    if janela > 1:
        audio = np.convolve(audio, np.ones(janela, dtype=np.float32) / janela, mode="same")
    n_saida = int(audio.size / fator)
    x_saida = np.arange(n_saida) * fator
    return np.interp(x_saida, np.arange(audio.size), audio).astype(np.float32)


def _ponto_de_corte(audio: np.ndarray, taxa: int, minimo_s, maximo_s, pausa_s):
    """Amostra onde cortar o áudio: no meio da 1ª pausa de fala (pelo menos pausa_s
    de silêncio) depois de minimo_s. Sem pausa e já com maximo_s, corta no momento
    mais silencioso dos últimos segundos. Senão, None (espera mais áudio)."""
    quadro = int(taxa * 0.03)  # quadros de 30 ms
    n = audio.size // quadro
    if n < 2:
        return None
    rms = np.sqrt(np.mean(audio[:n * quadro].reshape(n, quadro) ** 2, axis=1))
    # "Silêncio" é relativo ao próprio microfone: perto do ruído de fundo, longe da voz.
    piso, voz = np.percentile(rms, 10), np.percentile(rms, 90)
    quieto = rms < piso + (voz - piso) * 0.15
    precisa = max(1, round(pausa_s / 0.03))
    inicio = round(minimo_s / 0.03)
    seguidos = 0
    for i in range(n):
        seguidos = seguidos + 1 if quieto[i] else 0
        if seguidos >= precisa and i >= inicio:
            return (i - seguidos // 2) * quadro
    if audio.size < maximo_s * taxa:
        return None
    janela = max(1, round(0.3 / 0.03))
    media = np.convolve(rms, np.ones(janela) / janela, mode="same")
    de = max(inicio, n - round(10 / 0.03))
    return (de + int(np.argmin(media[de:]))) * quadro


class Gravador:
    """Grava do microfone para a memória. Uso: iniciar(indice) ... parar() -> array."""

    def __init__(self):
        self._stream = None
        self._blocos = []
        self._trava = threading.Lock()
        self._taxa = TAXA
        self.nivel = 0.0   # 0..1, para o medidor da interface
        self._inicio = 0.0

    @property
    def gravando(self) -> bool:
        return self._stream is not None

    @property
    def duracao(self) -> float:
        return time.monotonic() - self._inicio if self.gravando else 0.0

    def iniciar(self, indice):
        if self.gravando:
            return
        sd = _sd()
        if privacidade_bloqueada():
            raise ErroMicrofone(MSG_PRIVACIDADE)
        try:
            info = sd.query_devices(indice, "input")
        except Exception as erro:
            raise ErroMicrofone(_traduzir_erro(erro)) from erro

        # Tenta 16 kHz mono direto; se o driver não aceitar (WASAPI só aceita a
        # taxa nativa), grava na taxa nativa e converte para 16 kHz ao parar.
        taxa_nativa = int(info["default_samplerate"])
        tentativas = [(TAXA, 1), (taxa_nativa, 1),
                      (taxa_nativa, min(2, int(info["max_input_channels"])))]
        ultimo_erro = None
        for taxa, canais in tentativas:
            try:
                sd.check_input_settings(device=indice, samplerate=taxa,
                                        channels=canais, dtype="float32")
            except Exception as erro:
                ultimo_erro = erro
                continue
            try:
                self._blocos = []
                self._taxa = taxa
                stream = sd.InputStream(device=indice, samplerate=taxa, channels=canais,
                                        dtype="float32", callback=self._callback)
                stream.start()
            except Exception as erro:
                ultimo_erro = erro
                continue
            self._stream = stream
            self._inicio = time.monotonic()
            return
        raise ErroMicrofone(_traduzir_erro(ultimo_erro or RuntimeError("configuração não aceita")))

    def _callback(self, indata, frames, tempo, status):
        mono = indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0]
        mono = mono.astype(np.float32, copy=True)
        with self._trava:
            self._blocos.append(mono)
        # nível em escala "visual" (raiz do RMS deixa a voz baixa mais visível)
        rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
        self.nivel = min(1.0, (rms * 4) ** 0.5)

    def retirar_trecho(self, minimo_s=15.0, maximo_s=28.0, pausa_s=0.6):
        """Para transcrever enquanto a pessoa fala: com pelo menos minimo_s gravados,
        corta numa pausa e devolve esse começo (16 kHz mono); o resto continua na
        gravação. Retorna None se ainda não é hora de cortar."""
        with self._trava:
            n_blocos = len(self._blocos)
            if sum(b.size for b in self._blocos) < minimo_s * self._taxa:
                return None
            audio = np.concatenate(self._blocos)
        corte = _ponto_de_corte(audio, self._taxa, minimo_s, maximo_s, pausa_s)
        if corte is None:
            return None
        with self._trava:  # blocos que chegaram durante o cálculo ficam depois do resto
            self._blocos = [audio[corte:]] + self._blocos[n_blocos:]
        return _reamostrar(audio[:corte], self._taxa)

    def parar(self) -> np.ndarray:
        """Para a gravação e devolve o áudio em 16 kHz mono float32."""
        stream, self._stream = self._stream, None
        self.nivel = 0.0
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        with self._trava:
            blocos, self._blocos = self._blocos, []
        if not blocos:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(blocos)
        return _reamostrar(audio, self._taxa)

    def cancelar(self):
        self.parar()
