"""
Integração com o Windows (via ctypes, sem dependências extras):

  • AtalhoGlobal: Ctrl+Shift+Espaço funcionando em qualquer programa. Usa
    RegisterHotKey: o Windows só avisa o app quando ESSA combinação é apertada;
    nenhuma outra tecla é lida ou registrada. A thread fica dormindo (0% de CPU).
  • Esc segurado: só DURANTE uma gravação o app consulta se o Esc está apertado
    (para descartar o ditado). Não intercepta a tecla: o outro programa recebe o Esc.
  • Colagem no programa ativo (simula Ctrl+V), instância única.
  • Bandeja: ícone perto do relógio (pystray).
"""

import ctypes
import ctypes.wintypes as wt
import os
import threading
import traceback

user32 = ctypes.windll.user32
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Assinaturas explícitas: no Windows 64 bits, handles não cabem em "int".
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetParent.argtypes = [wt.HWND]
user32.GetParent.restype = wt.HWND
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.MonitorFromWindow.restype = wt.HANDLE
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.c_void_p]
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateEventW.restype = wt.HANDLE
kernel32.OpenEventW.restype = wt.HANDLE
kernel32.SetEvent.argtypes = [wt.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]

WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0002, 0x0004, 0x4000
VK_SPACE, VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN, VK_V, VK_ESCAPE = (
    0x20, 0x10, 0x11, 0x12, 0x5B, 0x5C, 0x56, 0x1B)
KEYEVENTF_KEYUP = 0x0002


# ---------------------------------------------------------------- atalho global

class AtalhoGlobal:
    """Registra Ctrl+Shift+Espaço. ao_apertar() é chamado NA THREAD DO ATALHO."""

    def __init__(self, ao_apertar):
        self.ao_apertar = ao_apertar
        self.ativo = False
        self._tid = None
        self._pronto = threading.Event()
        self._thread = None

    def iniciar(self) -> bool:
        """Retorna False se outro programa já usa essa combinação."""
        self._thread = threading.Thread(target=self._rodar, name="atalho", daemon=True)
        self._thread.start()
        self._pronto.wait(3)
        return self.ativo

    def _rodar(self):
        self._tid = kernel32.GetCurrentThreadId()
        self.ativo = bool(user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT,
                                                VK_SPACE))
        self._pronto.set()
        if not self.ativo:
            return
        msg = wt.MSG()
        try:
            # GetMessage dorme até chegar uma mensagem: nenhum consumo de CPU.
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self.ao_apertar()
                    except Exception:
                        traceback.print_exc()
        finally:
            user32.UnregisterHotKey(None, 1)
            self.ativo = False

    def parar(self):
        if self._tid and self.ativo:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)


# ---------------------------------------------------------------- janelas / teclado

def janela_ativa():
    return user32.GetForegroundWindow()


def janela_e_deste_app(hwnd) -> bool:
    if not hwnd:
        return False
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == os.getpid()


def teclas_de_atalho_soltas() -> bool:
    """True quando Ctrl, Shift, Alt, Win e Espaço já foram soltos (para o Ctrl+V
    simulado não virar Ctrl+Shift+V)."""
    return not any(user32.GetAsyncKeyState(vk) & 0x8000
                   for vk in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN, VK_SPACE))


def esc_apertado() -> bool:
    return bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)


def simular_ctrl_v():
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", _RECT), ("rcWork", _RECT),
                ("dwFlags", wt.DWORD)]


def area_de_trabalho(hwnd=None):
    """(esquerda, topo, direita, baixo) da área útil (sem a barra de tarefas) do
    monitor onde está a janela hwnd (ou do monitor principal)."""
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    monitor = user32.MonitorFromWindow(hwnd or user32.GetForegroundWindow(), 2)  # 2 = mais próximo
    if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        r = info.rcWork
        return r.left, r.top, r.right, r.bottom
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def janela_flutuante_sem_foco(widget_tk):
    """Faz uma Toplevel (overrideredirect) nunca roubar o foco e não aparecer
    no Alt+Tab. Cantos arredondados no Windows 11."""
    hwnd = user32.GetParent(widget_tk.winfo_id()) or widget_tk.winfo_id()
    GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x08000000, 0x00000080
    estilo = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, estilo | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    try:
        canto = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd), 33, ctypes.byref(canto), 4)
    except Exception:
        pass


# ---------------------------------------------------------------- instância única

class InstanciaUnica:
    """Garante um só app aberto. Se já houver um, avisa-o (para mostrar a janela)."""

    def __init__(self, nome):
        self._nome_evento = f"Local\\{nome}.Mostrar"
        self._mutex = kernel32.CreateMutexW(None, False, f"Local\\{nome}.Mutex")
        self.primeira = ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
        self._evento = None
        self._parar = False

    def avisar_a_outra(self):
        h = kernel32.OpenEventW(0x0002, False, self._nome_evento)  # EVENT_MODIFY_STATE
        if h:
            kernel32.SetEvent(h)
            kernel32.CloseHandle(h)

    def esperar_avisos(self, ao_receber):
        """Thread dormindo até outra instância pedir para mostrar a janela."""
        self._evento = kernel32.CreateEventW(None, False, False, self._nome_evento)

        def laco():
            while not self._parar:
                kernel32.WaitForSingleObject(self._evento, 0xFFFFFFFF)  # INFINITE
                if not self._parar:
                    ao_receber()
        threading.Thread(target=laco, name="instancia", daemon=True).start()

    def encerrar(self):
        self._parar = True
        if self._evento:
            kernel32.SetEvent(self._evento)


# ---------------------------------------------------------------- bandeja

class Bandeja:
    """Ícone perto do relógio. As ações são chamadas na thread do pystray."""

    def __init__(self, imagem_rgba, ao_abrir, ao_ditar, ao_sair):
        import pystray
        from PIL import Image
        self._Image = Image
        self.icone = pystray.Icon(
            "TranscricaoAudio", Image.fromarray(imagem_rgba), "Transcrição de Áudio",
            menu=pystray.Menu(
                pystray.MenuItem("Abrir", lambda: ao_abrir(), default=True),
                pystray.MenuItem("Ditar  (Ctrl+Shift+Espaço)", lambda: ao_ditar()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Sair", lambda: ao_sair())))
        self._thread = threading.Thread(target=self.icone.run, name="bandeja", daemon=True)

    def iniciar(self):
        self._thread.start()

    def trocar_imagem(self, imagem_rgba):
        try:
            self.icone.icon = self._Image.fromarray(imagem_rgba)
        except Exception:
            traceback.print_exc()

    def notificar(self, mensagem, titulo="Transcrição de Áudio"):
        try:
            self.icone.notify(mensagem, titulo)
        except Exception:
            traceback.print_exc()

    def parar(self):
        try:
            self.icone.stop()
        except Exception:
            pass
        self._thread.join(timeout=2)
