#!/usr/bin/env python3
"""
Touchpad remoto: controla el mouse de tu PC Linux desde el celular.

- Crea un mouse virtual con uinput (funciona en Wayland/Hyprland/X11).
- Sirve una página web; el celular se conecta por WebSocket.

Uso:  python3 touchpad.py [--port 8765] [--dry-run]
"""
import argparse
import asyncio
import errno
import pathlib
import secrets
import shutil
import socket
import sys

from aiohttp import web, WSMsgType

HERE = pathlib.Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# Mouse virtual
# --------------------------------------------------------------------------
class VirtualMouse:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.rx = self.ry = 0.0  # restos sub-pixel
        self.wy = self.wx = 0.0  # acumuladores de scroll hi-res
        self.wy_lo = self.wx_lo = 0  # para emitir REL_WHEEL clásico cada 120
        self.held = set()

        if dry_run:
            self.ui = None
            return

        from evdev import UInput, ecodes as e

        self.e = e
        hi_v = getattr(e, "REL_WHEEL_HI_RES", 11)
        hi_h = getattr(e, "REL_HWHEEL_HI_RES", 12)
        self.REL_WHEEL_HI_RES, self.REL_HWHEEL_HI_RES = hi_v, hi_h
        self.BTN = {"l": e.BTN_LEFT, "r": e.BTN_RIGHT, "m": e.BTN_MIDDLE}
        cap = {
            e.EV_KEY: list(self.BTN.values()),
            e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL, hi_v, hi_h],
        }
        self.ui = UInput(cap, name="Phone Touchpad", vendor=0x1234, product=0x5678)

    def _log(self, *a):
        print("[dry-run]", *a)

    def move(self, dx, dy):
        self.rx += dx
        self.ry += dy
        ix, iy = int(self.rx), int(self.ry)
        if not ix and not iy:
            return
        self.rx -= ix
        self.ry -= iy
        if self.dry_run:
            return self._log("move", ix, iy)
        e = self.e
        if ix:
            self.ui.write(e.EV_REL, e.REL_X, ix)
        if iy:
            self.ui.write(e.EV_REL, e.REL_Y, iy)
        self.ui.syn()

    def scroll(self, dx, dy):
        """dx/dy en 'unidades hi-res' (120 = un paso de rueda)."""
        self.wy += dy
        self.wx += dx
        iy, ix = int(self.wy), int(self.wx)
        if not iy and not ix:
            return
        self.wy -= iy
        self.wx -= ix
        if self.dry_run:
            return self._log("scroll", ix, iy)
        e = self.e
        if iy:
            self.ui.write(e.EV_REL, self.REL_WHEEL_HI_RES, iy)
            self.wy_lo += iy
            steps = int(self.wy_lo / 120)
            if steps:
                self.wy_lo -= steps * 120
                self.ui.write(e.EV_REL, e.REL_WHEEL, steps)
        if ix:
            self.ui.write(e.EV_REL, self.REL_HWHEEL_HI_RES, ix)
            self.wx_lo += ix
            steps = int(self.wx_lo / 120)
            if steps:
                self.wx_lo -= steps * 120
                self.ui.write(e.EV_REL, e.REL_HWHEEL, steps)
        self.ui.syn()

    def button(self, b, down):
        if b not in ("l", "r", "m"):
            return
        (self.held.add if down else self.held.discard)(b)
        if self.dry_run:
            return self._log("button", b, "down" if down else "up")
        self.ui.write(self.e.EV_KEY, self.BTN[b], 1 if down else 0)
        self.ui.syn()

    def click(self, b):
        self.button(b, True)
        self.button(b, False)

    def release_all(self):
        for b in list(self.held):
            self.button(b, False)


# --------------------------------------------------------------------------
# Teclado virtual
# --------------------------------------------------------------------------
# Distribución US: carácter -> (nombre de tecla, requiere shift).
# Se usa para atajos (Ctrl+C, Super+Q...) y como respaldo si no hay wtype.
_US_PLAIN = {
    **{c: c.upper() for c in "abcdefghijklmnopqrstuvwxyz"},
    **{c: c for c in "0123456789"},
    " ": "SPACE", "-": "MINUS", "=": "EQUAL", "[": "LEFTBRACE", "]": "RIGHTBRACE",
    "\\": "BACKSLASH", ";": "SEMICOLON", "'": "APOSTROPHE", "`": "GRAVE",
    ",": "COMMA", ".": "DOT", "/": "SLASH", "\t": "TAB", "\n": "ENTER",
}
_US_SHIFT = {
    **{c.upper(): c.upper() for c in "abcdefghijklmnopqrstuvwxyz"},
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "MINUS", "+": "EQUAL", "{": "LEFTBRACE",
    "}": "RIGHTBRACE", "|": "BACKSLASH", ":": "SEMICOLON", '"': "APOSTROPHE",
    "~": "GRAVE", "<": "COMMA", ">": "DOT", "?": "SLASH",
}
US_MAP = {**{c: (k, False) for c, k in _US_PLAIN.items()},
          **{c: (k, True) for c, k in _US_SHIFT.items()}}

SPECIAL = {
    "esc": "ESC", "tab": "TAB", "enter": "ENTER", "backspace": "BACKSPACE",
    "delete": "DELETE", "left": "LEFT", "right": "RIGHT", "up": "UP", "down": "DOWN",
    "home": "HOME", "end": "END", "pageup": "PAGEUP", "pagedown": "PAGEDOWN",
    "space": "SPACE", **{f"f{i}": f"F{i}" for i in range(1, 13)},
}
MODS = {"ctrl": "LEFTCTRL", "alt": "LEFTALT", "shift": "LEFTSHIFT", "super": "LEFTMETA"}


class VirtualKeyboard:
    def __init__(self, dry_run=False, use_wtype=True):
        self.dry_run = dry_run
        self.wtype = shutil.which("wtype") if use_wtype else None
        if dry_run:
            self.ui = None
            return
        from evdev import UInput, ecodes as e

        self.e = e
        names = {k for k, _ in US_MAP.values()} | set(SPECIAL.values()) | set(MODS.values())
        self.code = {n: getattr(e, "KEY_" + n) for n in names}
        self.ui = UInput({e.EV_KEY: list(self.code.values())}, name="Phone Keyboard",
                         vendor=0x1234, product=0x5679)

    def _log(self, *a):
        print("[dry-run]", *a)

    def _tap(self, key, mods=()):
        if self.dry_run:
            return self._log("key", "+".join([*mods, key]))
        e, c = self.e, self.code
        for m in mods:
            self.ui.write(e.EV_KEY, c[m], 1)
        self.ui.write(e.EV_KEY, c[key], 1)
        self.ui.syn()
        self.ui.write(e.EV_KEY, c[key], 0)
        for m in reversed(mods):
            self.ui.write(e.EV_KEY, c[m], 0)
        self.ui.syn()

    def key(self, mods, key):
        """mods: 'ctrl+alt'... ; key: nombre especial, un carácter, o '' (solo modificador)."""
        mods = [MODS[m] for m in mods.split("+") if m in MODS]
        if key in SPECIAL:
            return self._tap(SPECIAL[key], mods)
        if key == "" and mods:  # p. ej. Super solo
            return self._tap(mods[-1], mods[:-1])
        k = US_MAP.get(key) or US_MAP.get(key.lower())
        if k:
            name, shift = k
            if shift and "LEFTSHIFT" not in mods:
                mods.append("LEFTSHIFT")
            self._tap(name, mods)

    async def type_text(self, text):
        # Los Enter van por uinput; el resto del texto por wtype (respeta acentos, ñ, etc.)
        for i, chunk in enumerate(text.split("\n")):
            if i:
                self._tap("ENTER")
            if not chunk:
                continue
            if self.dry_run:
                self._log("type", repr(chunk))
            elif self.wtype:
                proc = await asyncio.create_subprocess_exec(
                    self.wtype, "-", stdin=asyncio.subprocess.PIPE)
                await proc.communicate(chunk.encode())
            else:
                for ch in chunk:
                    k = US_MAP.get(ch)
                    if k:
                        self._tap(k[0], ["LEFTSHIFT"] if k[1] else [])
                    else:
                        print(f"No puedo tipear {ch!r} sin wtype (sudo dnf install wtype)")


# --------------------------------------------------------------------------
# Servidor web + WebSocket
# --------------------------------------------------------------------------
def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no envía paquetes
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def make_app(mouse: VirtualMouse, kb: VirtualKeyboard, token: str):
    async def index(request):
        if request.query.get("t") != token:
            return web.Response(status=403, text="Token inválido")
        return web.FileResponse(HERE / "index.html")

    async def ws_handler(request):
        if request.query.get("t") != token:
            return web.Response(status=403)
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        sock = request.transport.get_extra_info("socket")
        if sock is not None:  # sin Nagle -> menor latencia
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print(f"Conectado: {request.remote}")
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = msg.data
                if data.startswith("t,"):  # texto: el mensaje entero, sin separar
                    await kb.type_text(data[2:])
                    continue
                if data.startswith("k,"):  # tecla: k,<mods>,<tecla>
                    p = data.split(",", 2)
                    if len(p) == 3:
                        kb.key(p[1], p[2])
                    continue
                # Varios comandos de mouse por mensaje separados por ';'
                for cmd in data.split(";"):
                    p = cmd.split(",")
                    try:
                        op = p[0]
                        if op == "m":
                            mouse.move(float(p[1]), float(p[2]))
                        elif op == "s":
                            mouse.scroll(float(p[1]), float(p[2]))
                        elif op == "c":
                            mouse.click(p[1])
                        elif op == "d":
                            mouse.button(p[1], True)
                        elif op == "u":
                            mouse.button(p[1], False)
                    except (IndexError, ValueError):
                        pass
        finally:
            mouse.release_all()
            print(f"Desconectado: {request.remote}")
        return ws

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    return app


def open_socket(host, port, tries=20):
    """Abre el socket de escucha. Si el puerto está ocupado prueba los siguientes."""
    for p in range(port, port + tries):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, p))
        except OSError as err:
            sock.close()
            if err.errno != errno.EADDRINUSE:
                raise
            if p == port:
                print(f"\n⚠  El puerto {port} ya está en uso (¿quedó otra instancia abierta?).")
                print(f"   Para ver qué lo usa:  ss -ltnp 'sport = :{port}'")
            continue
        sock.listen(128)
        sock.setblocking(False)
        if p != port:
            print(f"   Usando el puerto {p} en su lugar. Abrilo en el firewall:")
            print(f"   sudo firewall-cmd --permanent --add-port={p}/tcp && sudo firewall-cmd --reload")
        return sock, p
    sys.exit(f"No encontré un puerto libre entre {port} y {port + tries - 1}.")


def main():
    ap = argparse.ArgumentParser(description="Touchpad remoto vía celular")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--token", help="token fijo (por defecto uno aleatorio)")
    ap.add_argument("--no-wtype", action="store_true",
                    help="tipear texto directo por uinput (distribución US)")
    ap.add_argument("--dry-run", action="store_true", help="no mueve el mouse, solo imprime")
    args = ap.parse_args()

    token = args.token or secrets.token_urlsafe(6)
    sock, port = open_socket(args.host, args.port)
    mouse = VirtualMouse(dry_run=args.dry_run)
    kb = VirtualKeyboard(dry_run=args.dry_run, use_wtype=not args.no_wtype)
    if not args.dry_run and not kb.wtype:
        print("⚠  wtype no está instalado: el texto se tipea con distribución US")
        print("   (sin acentos ni ñ). Instalalo con:  sudo dnf install wtype")
    url = f"http://{local_ip()}:{port}/?t={token}"

    print("\nAbrí esta URL en el celular (misma red Wi-Fi):\n")
    print("   ", url, "\n")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except ImportError:
        print("(Tip: instalá python3-qrcode para ver un QR acá)")

    web.run_app(make_app(mouse, kb, token), sock=sock, print=None)


if __name__ == "__main__":
    main()
