# mouse-controller

Usá el iPhone como touchpad y teclado de tu PC Linux (Wayland/Hyprland/X11).

## Instalación (Fedora)
    sudo dnf install -y gcc python3-devel kernel-headers wtype
    python3 -m venv .venv && .venv/bin/pip install aiohttp evdev qrcode

Permiso para crear el mouse/teclado virtual sin sudo (después cerrar sesión y volver a entrar):
    echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' | sudo tee /etc/udev/rules.d/99-uinput.rules
    sudo usermod -aG input $USER
    echo uinput | sudo tee /etc/modules-load.d/uinput.conf

Firewall:
    sudo firewall-cmd --permanent --add-port=8765/tcp && sudo firewall-cmd --reload

## Uso
    .venv/bin/python touchpad.py --token casa

Escaneá el QR con el iPhone (misma red Wi-Fi). Pensado para usarlo en horizontal.
En Safari: Compartir → "Agregar a inicio" para pantalla completa.

## Touchpad
- 1 dedo: mover · tap: click izquierdo
- 2 dedos: scroll · tap con 2 dedos: click derecho
- tap con 3 dedos: click medio
- tap y enseguida apoyar y arrastrar: drag
- Botones: Izq / Der / Mantener (deja apretado el izquierdo)

## Teclado
- **⌨ Teclado** abre el teclado del iPhone; lo que escribas se tipea en la PC.
- Fila de teclas especiales: Esc, Tab, flechas, ⌫, ⏎.
- **Ctrl / Alt / Super** quedan apretadas hasta la próxima tecla (Ctrl → c = Ctrl+C).
  Tocar Super dos veces manda Super solo.
- El texto se tipea con `wtype`, que respeta acentos y ñ con cualquier distribución.
  Sin `wtype` se usa distribución US (`--no-wtype` lo fuerza).
