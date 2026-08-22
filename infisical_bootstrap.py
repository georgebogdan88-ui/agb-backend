"""Bootstrap: incarca secretele din Infisical (secrets manager, proiectul
"AGB Agroparts Solution") in os.environ, cat mai devreme posibil in server.py
- inainte ca restul fisierului sa citeasca vreo variabila (BREVO_API_KEY,
MONGO_URL etc.) prin os.environ.get(...)/os.environ[...].

Complet opt-in si fail-safe:
  - Daca INFISICAL_CLIENT_ID / INFISICAL_CLIENT_SECRET nu sunt setate in
    mediu, load_secrets_from_infisical() nu face NICIO actiune - nici macar
    nu instantiaza clientul SDK. server.py continua exact ca azi, citind
    direct din .env / mediul deja existent. Asta e critic: productia de azi
    (Render) NU are aceste variabile setate, deci codul asta nu trebuie sa
    schimbe nimic acolo pana cand George le adauga explicit.
  - Daca ambele sunt setate (Machine Identity "agb-backend-render" cu
    Universal Auth), citeste secretele din folderul /agb-backend al
    proiectului Infisical si le pune in os.environ cu
    os.environ.setdefault(...) - o valoare deja setata local (.env / mediul
    procesului) are mereu prioritate fata de Infisical, util pentru debug
    local fara sa depinzi mereu de retea.
  - Orice eroare (retea cazuta, credentiale gresite, proiect/folder
    inexistent, orice altceva) e prinsa si doar logata ca warning - NU
    trebuie niciodata sa opreasca pornirea serverului. La esec, comportamentul
    e identic cu "Infisical nici nu ar fi configurat" (fallback total la
    .env / mediul existent).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Proiectul Infisical "AGB Agroparts Solution" - ID-ul de proiect NU e secret
# (vizibil oricui are acces la proiect in dashboard-ul Infisical), doar
# Client Secret-ul Machine Identity-ului e secret si nu trebuie sa ajunga
# niciodata in cod/log-uri. Toate cele 3 valori de mai jos sunt suprascriabile
# prin variabile de mediu, pentru flexibilitate (alt environment/path fara
# schimbare de cod).
DEFAULT_INFISICAL_PROJECT_ID = "71eb9564-856a-4445-9bf0-599c19bb989b"
DEFAULT_INFISICAL_ENVIRONMENT = "dev"
DEFAULT_INFISICAL_SECRET_PATH = "/agb-backend"
INFISICAL_HOST = "https://app.infisical.com"


def load_secrets_from_infisical() -> int:
    """Populeaza os.environ cu secretele din Infisical, DOAR daca mecanismul
    e configurat (INFISICAL_CLIENT_ID + INFISICAL_CLIENT_SECRET prezente in
    mediu). Trebuie apelata cat mai devreme in server.py, inainte de prima
    citire a oricarei variabile de mediu applicative.

    Returneaza numarul de chei efectiv adaugate in os.environ (0 daca
    mecanismul nu e configurat, sau daca a esuat in orice fel - fallback
    total la .env / mediul deja existent).
    """
    client_id = os.environ.get("INFISICAL_CLIENT_ID")
    client_secret = os.environ.get("INFISICAL_CLIENT_SECRET")
    if not client_id or not client_secret:
        # Mecanism neconfigurat - nu atingem deloc os.environ si nici nu
        # instantiem clientul SDK. Comportament identic cu azi.
        return 0

    project_id = os.environ.get("INFISICAL_PROJECT_ID", DEFAULT_INFISICAL_PROJECT_ID)
    environment_slug = os.environ.get("INFISICAL_ENVIRONMENT", DEFAULT_INFISICAL_ENVIRONMENT)
    secret_path = os.environ.get("INFISICAL_SECRET_PATH", DEFAULT_INFISICAL_SECRET_PATH)

    try:
        from infisical_sdk import InfisicalSDKClient

        client = InfisicalSDKClient(host=INFISICAL_HOST)
        client.auth.universal_auth.login(client_id=client_id, client_secret=client_secret)

        response = client.secrets.list_secrets(
            project_id=project_id,
            environment_slug=environment_slug,
            secret_path=secret_path,
        )

        loaded = 0
        for secret in response.secrets:
            key = getattr(secret, "secretKey", None)
            value = getattr(secret, "secretValue", None)
            if not key or value is None:
                continue
            already_set = key in os.environ
            # setdefault: o valoare locala explicita (.env / mediul
            # procesului) are mereu prioritate fata de Infisical.
            os.environ.setdefault(key, value)
            if not already_set:
                loaded += 1

        logger.info(
            "Infisical: %d secret(e) incarcate din proiectul %s (environment=%s, path=%s)",
            loaded, project_id, environment_slug, secret_path,
        )
        return loaded
    except Exception as exc:  # noqa: BLE001 - orice eroare aici NU trebuie sa opreasca pornirea serverului
        logger.warning(
            "Infisical: mecanism configurat (INFISICAL_CLIENT_ID/SECRET prezente), dar incarcarea "
            "secretelor a esuat - continui cu .env / mediul existent, fara sa opresc pornirea. "
            "Detaliu: %s", exc,
        )
        return 0
