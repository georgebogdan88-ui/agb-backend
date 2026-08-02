# Teste de încărcare AGB — k6

Scripturi pregătite conform planului din auditul de scalabilitate. **Nimic din acest folder nu a fost rulat.** Rulați-le doar după ce:

1. Există un **mediu de staging separat** (servicii Render + bază de date proprii, NU cele de producție).
2. Baza de date de staging are **date sintetice** (produse/utilizatori generați, nu copiați din producție).
3. Ați citit și sunteți de acord cu scripturile (mai jos, explicat ce face fiecare).

Scripturile refuză singure să ruleze dacă `WEBSHOP_URL`/`BACKEND_URL` par a fi domenii de producție (`agb-backend.onrender.com` etc.) — vezi `config.js`.

## Dimensionare staging (obligatoriu de respectat)

Producția reală (confirmată direct din dashboard-ul Render, nu presupusă): `agb-backend` și `agb-webshop` rulează fiecare pe **Render Standard — 2GB RAM / 1 CPU / ~$25/lună**.

**Staging-ul trebuie să folosească exact același plan Standard (2GB/1 CPU) pentru ambele servicii** — nu un plan mai mic (ar da rezultate fals-pesimiste) și nu un plan mai mare (ar ascunde exact blocajul de "1 singur CPU" pe care raportul de scalabilitate l-a identificat ca prima limitare reală, prin apeluri `bcrypt` sincrone care blochează tot event loop-ul). Cu Standard identic producției, rezultatele k6 sunt direct comparabile cu ce rulează azi live.

Cost estimat pentru toată seria de teste (25→1000 + spike + soak, rulate pe parcursul a câtorva zile, servicii oprite după): **~$10-25** pentru partea Render (facturare pe zi, nu lunar), plus opțional ~$57-58 dacă se testează și un upgrade Atlas M0→M10 pentru comparație. Vezi planul complet de staging pentru detalii (variabile de mediu, protecții, date sintetice).

## Fișiere

| Fișier | Ce face |
|---|---|
| `config.js` | Configurare comună + verificarea de siguranță (refuză producția). |
| `scenario.js` | Fluxul realist de utilizator, folosit de toate scripturile: homepage → căutare → filtrare → pagină produs + imagine → login (30%) → adaugă în coș → vezi coș → creează comandă (10%). |
| `test-25.js` … `test-1000.js` | Cele 5 niveluri cerute, fiecare cu propriul ramp/durată/threshold. |
| `spike-test.js` | Creștere bruscă 50→500 în 20 secunde. |
| `soak-test.js` | Test de durată (2-4 ore, configurabil), sarcină constantă moderată (150 utilizatori). |
| `healthcheck.js` | Probă continuă de disponibilitate, de rulat separat în timpul unui test de recuperare (opriți manual un serviciu în staging și urmăriți acest script). |
| `verify_results.py` | Verificări post-test direct în baza de date de staging: comenzi duplicate, stoc negativ, coșuri rămase de la test. |

## Cum se rulează

Necesită [k6 instalat](https://k6.io/docs/get-started/installation/) (nu e inclus în acest workspace).

```bash
# Exemplu, nivel 100 utilizatori:
k6 run \
  --env WEBSHOP_URL=https://agb-webshop-staging.onrender.com \
  --env BACKEND_URL=https://agb-backend-staging.onrender.com/api \
  test-100.js

# Test de durată, 4 ore:
k6 run \
  --env WEBSHOP_URL=... --env BACKEND_URL=... \
  --env K6_SOAK_HOURS=4 \
  soak-test.js

# Test de recuperare — în terminal 1:
k6 run --env WEBSHOP_URL=... --env BACKEND_URL=... --duration 30m healthcheck.js
# ... în terminal 2, la minutul 10-15, opriți manual (controlat) serviciul
# backend din staging câteva minute, apoi reporniți-l, și urmăriți
# healthcheck.js pentru a vedea exact când a picat/revenit.
```

După orice test care creează comenzi:

```bash
python verify_results.py "mongodb+srv://.../staging_db"
```

## Variabile opționale (config.js)

| Variabilă | Implicit | Rol |
|---|---|---|
| `TEST_USER_EMAIL_PREFIX` | `loadtest-user-` | Prefixul contas de test folosite la login (trebuie să existe deja în staging, seedate separat). |
| `TEST_USER_PASSWORD` | `LoadTest123!` | Parola comună a conturilor de test. |
| `TEST_USER_COUNT` | `1000` | Câte conturi de test există (login-ul alege unul random din acest interval). |
| `SEARCH_TERMS` | `filtru,pompa,DZ100001,rulment,cuzineti` | Termeni de căutare folosiți (separați prin virgulă). |
| `COLLECTIONS` | `Piese noi,Piese din dezmembrare` | Colecții folosite la filtrare. |

## Indicatorii urmăriți

Nativ în k6 (apar automat în sumarul de la finalul rulării):
- `http_req_duration` (p50/p95/p99), `http_req_failed`, `http_reqs` — per pas, datorită tag-urilor `step:` din `scenario.js` (`homepage`, `search`, `filter`, `product_page`, `product_image`, `login`, `add_to_cart`, `view_cart`, `create_order`).
- Metrici custom: `login_success_rate`, `cart_add_success_rate`, `order_success_rate`, `orders_created_total`, `image_load_duration`, `search_page_duration`, `product_page_duration`.

**Trebuie corelate manual** (nu sunt vizibile din k6):
- CPU/RAM — dashboard Render, per serviciu, în fereastra exactă a testului.
- Conexiuni la baza de date — dashboard MongoDB Atlas (Metrics → Connections).
- Interogări lente — Atlas Profiler (de activat temporar pe clusterul de staging).
- Comenzi duplicate / stoc negativ / coșuri rămase — `verify_results.py`, după test.
- Joburi eșuate — log-urile Render ale serviciilor, verificare manuală.

## Notă despre rezervarea de stoc

Scenariul include un pas de "creează comandă", dar **verificarea de stoc și idempotency nu sunt încă implementate** în backend (rămân pe listă din auditurile anterioare) — deci testul nu poate verifica "stoc negativ" sau "2 comenzi simultane pentru ultima piesă" ca fenomene reale până când acel fix e implementat. `verify_results.py` are deja verificarea de stoc negativ pregătită, pentru ziua în care fix-ul există.
