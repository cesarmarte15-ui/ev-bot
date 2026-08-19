"""
Cache generico a disco + retry-with-backoff, compartido por mlb_schedule.py
y pybaseball_source.py. No sabe nada de MLB especificamente: solo persiste
el resultado de una funcion "cara" (llamada de red / scrape) bajo una
cache_path, y lo devuelve de ahi en corridas siguientes sin volver a pegarle
a la red.
"""
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("model.store")


def cached_call(cache_path: Path, fetch_fn: Callable[[], Any], fmt: str = "parquet") -> Any:
    """
    Si cache_path existe, carga y devuelve el contenido cacheado (sin llamar
    fetch_fn). Si no existe, llama fetch_fn(), persiste el resultado en
    cache_path segun 'fmt' (parquet | json | csv) y lo devuelve.

    fmt="parquet"/"csv" espera que fetch_fn() devuelva un pandas DataFrame.
    fmt="json" espera un objeto serializable con json.dump (dict/list).
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if cache_path.exists():
        if fmt == "parquet":
            import pandas as pd
            return pd.read_parquet(cache_path)
        if fmt == "csv":
            import pandas as pd
            return pd.read_csv(cache_path)
        if fmt == "json":
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        raise ValueError(f"fmt desconocido: {fmt}")

    result = fetch_fn()

    if fmt == "parquet":
        result.to_parquet(cache_path)
    elif fmt == "csv":
        result.to_csv(cache_path, index=False)
    elif fmt == "json":
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(result, f)
    else:
        raise ValueError(f"fmt desconocido: {fmt}")

    return result


def retry_with_backoff(
    fn: Callable[[], Any],
    attempts: int = 3,
    base_delay: float = 5.0,
    on_giveup: Callable[[Exception], None] | None = None,
) -> Any:
    """
    Reintenta fn() hasta 'attempts' veces con backoff exponencial simple
    (base_delay * 2**intento). Si se agotan los intentos, llama on_giveup(e)
    si se paso (para que el caller pueda anotar la falla en vez de abortar
    todo el run) y relanza la ultima excepcion.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < attempts - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning("Intento %d/%d fallo (%s), reintentando en %.1fs", attempt + 1, attempts, e, delay)
                time.sleep(delay)
            else:
                logger.error("Se agotaron los %d intentos: %s", attempts, e)

    if on_giveup is not None and last_exc is not None:
        on_giveup(last_exc)
    raise last_exc


def append_failed_key(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{key}\n")


def read_failed_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}
