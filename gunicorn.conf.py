# Gunicorn lee este archivo automaticamente si se arranca desde la raiz del
# repo (ej. start command "gunicorn app:app"). Un solo worker a proposito:
# la cache de cuotas y el contador de creditos viven en memoria del proceso;
# con varios workers cada uno pediria sus propias cuotas y el consumo de
# creditos de The Odds API se multiplicaria. Los threads cubren la
# concurrencia de requests. Flags en el start command de Render pisan esto.
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
timeout = 120
