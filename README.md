# Pro Sports EV Bot - PC + iPhone

Este bot funciona como una web app local. Lo ejecutas en tu PC y puedes abrirlo desde:

- PC: http://127.0.0.1:5000
- iPhone en la misma red Wi-Fi: http://IP_DE_TU_PC:5000

## 1. Crear cuenta en The Odds API

Entra aquí:
https://the-odds-api.com/

Crea una cuenta y copia tu API Key.

## 2. Instalar Python en PC

Descarga Python:
https://www.python.org/downloads/

Durante la instalación marca:
Add Python to PATH

## 3. Preparar el bot

Abre la carpeta del bot en tu PC.

En Windows, presiona clic derecho dentro de la carpeta y abre Terminal.

Ejecuta:

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## 4. Configurar tu API Key

Copia el archivo:

```text
.env.example
```

y cámbiale el nombre a:

```text
.env
```

Luego abre `.env` y escribe tu clave:

```text
ODDS_API_KEY=tu_clave_real_aqui
EV_MIN=0.03
EDGE_MIN=0.03
```

## 5. Ejecutar en PC

```bash
python app.py
```

Luego abre:

```text
http://127.0.0.1:5000
```

## 6. Usar en iPhone

Tu PC y tu iPhone deben estar conectados al mismo Wi-Fi.

En la PC busca tu IP:

```bash
ipconfig
```

Busca algo como:

```text
IPv4 Address . . . . . . . . . . : 192.168.1.25
```

En tu iPhone abre Safari y entra a:

```text
http://192.168.1.25:5000
```

Cambia `192.168.1.25` por la IP real de tu PC.

## 7. Agregar al inicio del iPhone

En Safari:

1. Toca el botón Compartir
2. Toca "Agregar a pantalla de inicio"
3. Ponle nombre: EV Bot
4. Toca Agregar

## Qué detecta

- MLB
- NBA
- NHL
- NFL
- Soccer
- Moneyline
- Spread
- Totals
- EV positivo
- Edge alto
- Mejores cuotas disponibles

## Nota importante

Este bot encuentra valor esperado basado en odds del mercado. No garantiza resultados. Sirve para filtrar mejores oportunidades, no para asegurar ganancias.
