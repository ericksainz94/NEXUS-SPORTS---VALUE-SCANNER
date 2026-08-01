import requests
import time
import csv
import json
import os
from datetime import datetime, timedelta
import pytz

# ======================================================================
# NEXUS SPORTS - VALUE SCANNER (version honesta, basada en datos reales)
# ======================================================================
#
# QUE HACE ESTE BOT:
#   Compara las cuotas de varias casas de apuestas para el mismo evento,
#   calcula la probabilidad implicita "justa" (quitando el margen/vig de
#   cada casa) y solo avisa cuando la MEJOR cuota disponible en el mercado
#   paga mas de lo que esa probabilidad justa sugiere que deberia pagar.
#   Eso es una ineficiencia de mercado real y medible (value betting).
#
# QUE NO HACE:
#   No predice quien va a ganar. No inventa porcentajes de "probabilidad
#   de acierto". No fuerza un pick por deporte solo porque "toca horario".
#   Si un dia no hay valor en tenis, simplemente no se manda nada de tenis
#   ese dia. Eso es intencional, no un error a corregir.
#
# LIMITES QUE DEBES CONOCER:
#   - El "edge" (ventaja) que detecta esta tecnica suele ser pequeno
#     (2%-8%). No existen los picks "casi seguros" en apuestas deportivas
#     legitimas.
#   - Esto NO garantiza ganancias en el corto plazo. La varianza en
#     apuestas deportivas es alta incluso con edge positivo real.
#   - Necesitas volumen de muestra grande (cientos de apuestas) para que
#     un edge del 3-5% se refleje en resultados. En pocas apuestas puedes
#     perder igual.
#   - Este bot NO verifica resultados de tus apuestas ni ajusta tu
#     bankroll automaticamente; eso te toca registrarlo tu para poder
#     medir tu ROI real con el tiempo.
#
# ======================================================================

TOKEN_TG = os.environ.get("TOKEN_TG", "")
API_KEY_ODDS = os.environ.get("API_KEY_ODDS", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
ZONA_HORARIA = pytz.timezone('America/Mazatlan')

# Umbral minimo de edge para considerar que vale la pena avisar.
# 0.03 = 3%. Subelo si quieres menos avisos pero de mayor calidad.
EDGE_MINIMO = 0.03

# Fraccion de Kelly a usar (Kelly completo es muy agresivo/volatil;
# 0.25-0.5 (Kelly fraccionado) es lo que usa la mayoria de bettors serios)
FRACCION_KELLY = 0.25

# Tope duro de bankroll por apuesta, pase lo que pase con Kelly
MAX_PORCENTAJE_BANKROLL = 0.03  # 3% maximo

# Umbral minimo de edge para un PARLAY de 2 patas (mas alto que el de picks
# individuales, porque combinar dos apuestas acumula incertidumbre: estamos
# asumiendo independencia entre los dos eventos y estimando la cuota
# combinada, no leyendola directo de una casa. Mas margen de seguridad.
EDGE_MINIMO_PARLAY = 0.05

# Ventana de horas hacia adelante en la que un partido se considera "real y
# operable" ahora (24-48h es prudente: como cada deporte se revisa una vez
# al dia, esto es suficiente para agarrar los partidos de hoy/mañana sin
# tomar partidos de pretemporada o de una temporada que ni ha arrancado,
# que la API a veces lista con mucha anticipacion pero que no son
# apostables todavia en la practica.
VENTANA_HORAS_MAXIMO = 48

BANKROLL_INICIAL = 500.0

# Archivo donde se guarda el historial de picks para que puedas medir tu
# ROI real con el tiempo. Se crea junto al script si no existe.
LOG_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "picks_log.csv")
# JSON que va a leer la pagina web (se sobreescribe cada corrida con el
# historial completo, para que el sitio siempre muestre todo lo enviado).
LOG_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "picks.json")
LOG_HEADERS = [
    "fecha_hora_envio", "deporte", "evento", "resultado", "cuota", "casa",
    "prob_justa_pct", "edge_pct", "fraccion_bankroll_pct", "monto_sugerido",
    "hora_inicio_evento",
    "resultado_real",       # LLENAR TU: GANO / PERDIO / PUSH
    "ganancia_perdida",     # LLENAR TU: monto real +/- despues del partido
]

# Archivo separado para el historial de parlays (2 patas), con su propia
# probabilidad combinada y cuota combinada.
PARLAY_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parlays_log.csv")
PARLAY_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "parlays.json")
PARLAY_HEADERS = [
    "fecha", "pierna_1", "pick_1", "cuota_1", "pierna_2", "pick_2", "cuota_2",
    "prob_combinada_pct", "cuota_combinada_estimada", "edge_estimado_pct",
    "resultado_real", "ganancia_perdida",
]

# Los 4 deportes activos. Cada uno se revisa en su horario; si no hay
# partidos o no hay valor ese dia, simplemente no se envia nada ese
# deporte ese dia -- no es necesario "activarlos" cada vez.
# El parlay (mas abajo) SOLO combina Futbol + NBA, sin importar que estos
# 4 esten activos -- eso es una eleccion deliberada, no una limitacion tecnica.
DEPORTES = {
    "FUTBOL":  {"sport_key": "soccer_epl",       "market": "totals",  "hora": "07:30", "emoji": "⚽"},
    "TENIS":   {"sport_key": "tennis_atp",        "market": "h2h",     "hora": "08:00", "emoji": "🎾"},
    "HOCKEY":  {"sport_key": "icehockey_nhl",     "market": "totals",  "hora": "15:30", "emoji": "🏒"},
    "NBA":     {"sport_key": "basketball_nba",    "market": "totals",  "hora": "16:00", "emoji": "🏀"},
}


class NexusSportsValue:
    def __init__(self):
        self.token = TOKEN_TG
        self.api_key = API_KEY_ODDS
        self.chat_id = CHAT_ID
        self.last_update_id = -1
        self.alarmas_enviadas = {d: False for d in DEPORTES}
        self.parlay_enviado_hoy = False
        self.bankroll = BANKROLL_INICIAL
        self.picks_log = []  # guardamos cada pick para que puedas revisar despues

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------
    def send_msg(self, text):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            requests.post(url, json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
        except Exception as e:
            print(f"Error enviando mensaje: {e}")

    def revisar_comandos(self):
        url = f"https://api.telegram.org/bot{self.token}/getUpdates?offset={self.last_update_id + 1}"
        try:
            resp = requests.get(url, timeout=5).json()
            for update in resp.get("result", []):
                self.last_update_id = update["update_id"]
                if "message" in update:
                    msg = update["message"].get("text", "").upper().strip()
                    if msg in ["STATUS", "INFO"]:
                        self.send_msg(
                            f"🛡️ NEXUS VALUE SCANNER activo.\n"
                            f"Bankroll de referencia: ${self.bankroll:.2f}\n"
                            f"Picks detectados en total: {len(self.picks_log)}\n"
                            f"Umbral de edge minimo: {EDGE_MINIMO*100:.0f}%"
                        )
                    elif msg == "LOG":
                        self.enviar_resumen_csv()
        except Exception as e:
            print(f"Error revisando comandos: {e}")

    # ------------------------------------------------------------------
    # Matematica del value betting (esto es lo que reemplaza al random())
    # ------------------------------------------------------------------
    @staticmethod
    def probabilidad_implicita(cuota_decimal):
        """Cuota decimal -> probabilidad implicita cruda (con vig incluido)."""
        if not cuota_decimal or cuota_decimal <= 1:
            return None
        return 1.0 / cuota_decimal

    @staticmethod
    def quitar_vig(probabilidades):
        """
        Recibe las probabilidades implicitas de TODOS los resultados posibles
        de un mismo mercado en una misma casa (ej: Over y Under) y las
        normaliza para que sumen 1.0, removiendo el margen de la casa.
        """
        total = sum(probabilidades)
        if total <= 0:
            return probabilidades
        return [p / total for p in probabilidades]

    def calcular_probabilidad_justa_consenso(self, nombre, punto, bookmakers, market_key):
        """
        Junta las cuotas de un mismo resultado EXACTO (ej: nombre='Over',
        punto=2.5) en todas las casas disponibles, quita el vig de cada
        casa por separado, y promedia esas probabilidades "justas" entre
        casas. Empareja por nombre Y punto juntos para no mezclar Over
        con Under (o un hándicap con otro).
        """
        probabilidades_justas = []
        for bookmaker in bookmakers:
            for market in bookmaker.get("markets", []):
                if market.get("key") != market_key:
                    continue
                outcomes = market.get("outcomes", [])
                if len(outcomes) < 2:
                    continue
                cuotas = [o.get("price") for o in outcomes]
                probs_crudas = [self.probabilidad_implicita(c) for c in cuotas]
                if any(p is None for p in probs_crudas):
                    continue
                probs_justas = self.quitar_vig(probs_crudas)
                for o, p_justa in zip(outcomes, probs_justas):
                    if o.get("name") == nombre and o.get("point") == punto:
                        probabilidades_justas.append(p_justa)

        if not probabilidades_justas:
            return None
        return sum(probabilidades_justas) / len(probabilidades_justas)

    def mejor_cuota_disponible(self, nombre, punto, bookmakers, market_key):
        mejor = None
        casa_mejor = None
        for bookmaker in bookmakers:
            for market in bookmaker.get("markets", []):
                if market.get("key") != market_key:
                    continue
                for o in market.get("outcomes", []):
                    if o.get("name") == nombre and o.get("point") == punto:
                        precio = o.get("price")
                        if precio and (mejor is None or precio > mejor):
                            mejor = precio
                            casa_mejor = bookmaker.get("title", "N/D")
        return mejor, casa_mejor

    def kelly_fraccionado(self, probabilidad_justa, cuota_decimal):
        """
        Formula de Kelly: f* = (b*p - q) / b
        b = cuota - 1 (ganancia neta por unidad apostada)
        p = probabilidad de ganar (nuestra estimacion justa)
        q = 1 - p
        Devuelve la fraccion del bankroll a apostar, ya con el
        recorte fraccionado y el tope maximo aplicados.
        """
        b = cuota_decimal - 1
        p = probabilidad_justa
        q = 1 - p
        if b <= 0:
            return 0
        f_completo = (b * p - q) / b
        if f_completo <= 0:
            return 0  # Kelly negativo = no hay edge real, no apostar
        f_ajustado = f_completo * FRACCION_KELLY
        return min(f_ajustado, MAX_PORCENTAJE_BANKROLL)

    def enviar_resumen_csv(self):
        if not os.path.isfile(LOG_CSV):
            self.send_msg("📭 Aun no hay picks registrados en el log.")
            return
        try:
            with open(LOG_CSV, mode="r", encoding="utf-8") as f:
                filas = list(csv.DictReader(f))
        except Exception as e:
            self.send_msg(f"⚠️ Error leyendo el log: {e}")
            return

        total = len(filas)
        con_resultado = [r for r in filas if r.get("resultado_real", "").strip()]
        ganados = [r for r in con_resultado if r.get("resultado_real", "").strip().upper() == "GANO"]

        resumen = (
            f"📊 *RESUMEN DE PICKS REGISTRADOS*\n"
            f"Total de picks enviados: {total}\n"
            f"Con resultado anotado: {len(con_resultado)}\n"
        )
        if con_resultado:
            winrate = (len(ganados) / len(con_resultado)) * 100
            ganancia_total = 0.0
            for r in con_resultado:
                try:
                    ganancia_total += float(r.get("ganancia_perdida", "0") or 0)
                except ValueError:
                    pass
            resumen += (
                f"Winrate (de los anotados): {winrate:.1f}%\n"
                f"Ganancia/perdida acumulada: ${ganancia_total:.2f}\n"
            )
        else:
            resumen += "\nAun no has llenado 'resultado_real' en el CSV para ningun pick.\n"
        resumen += f"\n📁 Archivo completo: {LOG_CSV}"
        self.send_msg(resumen)

    # ------------------------------------------------------------------
    # Registro en CSV (para que puedas medir tu ROI real con el tiempo)
    # ------------------------------------------------------------------
    def actualizar_json_web(self):
        """Lee el CSV completo y lo vuelca a JSON para que la pagina web
        (GitHub Pages) lo muestre. Se sobreescribe completo cada vez,
        asi el sitio siempre refleja el historial mas reciente."""
        if not os.path.isfile(LOG_CSV):
            return
        try:
            with open(LOG_CSV, mode="r", encoding="utf-8") as f:
                filas = list(csv.DictReader(f))
            os.makedirs(os.path.dirname(LOG_JSON), exist_ok=True)
            with open(LOG_JSON, mode="w", encoding="utf-8") as f:
                json.dump(filas, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error actualizando JSON web: {e}")

    def registrar_pick_csv(self, deporte, pick, monto):
        archivo_existe = os.path.isfile(LOG_CSV)
        try:
            with open(LOG_CSV, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not archivo_existe:
                    writer.writerow(LOG_HEADERS)
                writer.writerow([
                    datetime.now(ZONA_HORARIA).strftime("%Y-%m-%d %H:%M:%S"),
                    deporte,
                    pick["evento"],
                    pick["resultado"],
                    f"{pick['cuota']:.2f}",
                    pick["casa"],
                    f"{pick['prob_justa']*100:.1f}",
                    f"{pick['edge']*100:.1f}",
                    f"{pick['fraccion_bankroll']*100:.1f}",
                    f"{monto:.2f}",
                    pick["hora_inicio"].astimezone(ZONA_HORARIA).strftime("%Y-%m-%d %H:%M"),
                    "",  # resultado_real - lo llenas tu
                    "",  # ganancia_perdida - lo llenas tu
                ])
        except Exception as e:
            print(f"Error escribiendo en CSV: {e}")

    # ------------------------------------------------------------------
    # Escaneo de un deporte
    # ------------------------------------------------------------------
    def escanear_deporte(self, deporte):
        config = DEPORTES[deporte]
        sport_key = config["sport_key"]
        market_key = config["market"]

        url = (f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
               f"?apiKey={self.api_key}&regions=us,eu&markets={market_key}&oddsFormat=decimal")

        try:
            resp = requests.get(url, timeout=10)
            datos = resp.json()
        except Exception as e:
            self.send_msg(f"⚠️ {config['emoji']} {deporte}: error consultando la API ({e}).")
            return

        if resp.status_code != 200 or not isinstance(datos, list):
            self.send_msg(f"⚠️ {config['emoji']} {deporte}: la API no devolvio datos validos.")
            return

        if len(datos) == 0:
            self.send_msg(f"📭 {config['emoji']} {deporte}: no hay eventos disponibles ahorita.")
            return

        ahora_utc = datetime.now(pytz.UTC)
        ventana_maxima = ahora_utc + timedelta(hours=VENTANA_HORAS_MAXIMO)
        mejores_picks = []

        for evento in datos:
            commence_time_str = evento.get('commence_time')
            if not commence_time_str:
                continue
            try:
                tiempo_inicio = datetime.strptime(commence_time_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
            except Exception:
                continue
            if tiempo_inicio <= ahora_utc:
                continue  # ya empezo, no sirve
            if tiempo_inicio > ventana_maxima:
                continue  # muy lejano (pretemporada / temporada que ni arranca), no es real todavia

            bookmakers = evento.get("bookmakers", [])
            if len(bookmakers) < 2:
                continue  # necesitamos al menos 2 casas para comparar consenso

            equipo_local = evento.get('home_team', 'Local')
            equipo_visita = evento.get('away_team', 'Visita')

            # Juntamos todos los resultados distintos (nombre + punto juntos,
            # ej: ('Over', 2.5) y ('Under', 2.5) por separado, nunca mezclados)
            resultados_posibles = set()
            for bookmaker in bookmakers:
                for market in bookmaker.get("markets", []):
                    if market.get("key") != market_key:
                        continue
                    for o in market.get("outcomes", []):
                        resultados_posibles.add((o.get("name"), o.get("point")))

            for nombre, punto in resultados_posibles:
                if nombre is None:
                    continue
                prob_justa = self.calcular_probabilidad_justa_consenso(nombre, punto, bookmakers, market_key)
                if prob_justa is None:
                    continue
                mejor_cuota, casa = self.mejor_cuota_disponible(nombre, punto, bookmakers, market_key)
                if mejor_cuota is None:
                    continue

                # Valor esperado de apostar 1 unidad a la mejor cuota disponible
                ev = (mejor_cuota * prob_justa) - 1

                # Etiqueta clara para el mensaje: "Over 2.5", "Under 2.5",
                # "Manchester City -1.5", o solo el nombre si no hay punto (h2h)
                etiqueta_resultado = f"{nombre} {punto}" if punto is not None else nombre

                if ev >= EDGE_MINIMO:
                    fraccion_bankroll = self.kelly_fraccionado(prob_justa, mejor_cuota)
                    if fraccion_bankroll <= 0:
                        continue
                    mejores_picks.append({
                        "evento": f"{equipo_local} vs {equipo_visita}",
                        "resultado": etiqueta_resultado,
                        "cuota": mejor_cuota,
                        "casa": casa,
                        "prob_justa": prob_justa,
                        "edge": ev,
                        "fraccion_bankroll": fraccion_bankroll,
                        "hora_inicio": tiempo_inicio,
                    })

        if not mejores_picks:
            self.send_msg(
                f"📭 {config['emoji']} {deporte}: revisado, sin ineficiencias de mercado "
                f"por encima del {EDGE_MINIMO*100:.0f}% hoy. No se envia pick — "
                f"es preferible no apostar a forzar uno sin valor real."
            )
            return None

        # Ordenamos por edge y mandamos el mejor (puedes cambiar a [:2] si quieres mas de uno)
        mejores_picks.sort(key=lambda x: x["edge"], reverse=True)
        pick = mejores_picks[0]
        monto = self.bankroll * pick["fraccion_bankroll"]

        mensaje = (
            f"{config['emoji']} *PICK CON VALOR DETECTADO - {deporte}*\n\n"
            f"🆚 {pick['evento']}\n"
            f"🎯 Resultado: {pick['resultado']}\n"
            f"🏦 Mejor cuota: {pick['cuota']:.2f} (casa: {pick['casa']})\n"
            f"📐 Probabilidad justa de mercado (consenso, sin vig): {pick['prob_justa']*100:.1f}%\n"
            f"📈 Edge estimado: {pick['edge']*100:.1f}%\n"
            f"💰 Sugerido (Kelly {FRACCION_KELLY*100:.0f}%, tope {MAX_PORCENTAJE_BANKROLL*100:.0f}%): "
            f"{pick['fraccion_bankroll']*100:.1f}% del bankroll ≈ ${monto:.2f}\n"
            f"🕒 Inicio: {pick['hora_inicio'].astimezone(ZONA_HORARIA).strftime('%H:%M')} hora local\n\n"
            f"⚠️ Recuerda: un edge estadistico positivo no garantiza ganar ESTA apuesta. "
            f"Se cumple estadisticamente en el largo plazo con muchas apuestas registradas."
        )
        self.send_msg(mensaje)
        self.picks_log.append(pick)
        self.registrar_pick_csv(deporte, pick, monto)
        self.actualizar_json_web()
        pick["deporte"] = deporte
        return pick

    # ------------------------------------------------------------------
    # Parlay de 2 patas (SOLO combina picks que YA pasaron el filtro de
    # valor individualmente. Nunca busca "que sume x2/x3": calcula la
    # probabilidad combinada real y solo avisa si el parlay sigue
    # teniendo edge positivo despues de combinar. Si no lo tiene, te lo
    # dice tambien -- no lo oculta.
    # ------------------------------------------------------------------
    def leer_pick_de_hoy(self, deporte):
        """Busca en el CSV el pick mas reciente de HOY para ese deporte,
        siempre y cuando el partido todavia no haya empezado."""
        if not os.path.isfile(LOG_CSV):
            return None
        hoy_str = datetime.now(ZONA_HORARIA).strftime("%Y-%m-%d")
        ahora = datetime.now(ZONA_HORARIA)
        try:
            with open(LOG_CSV, mode="r", encoding="utf-8") as f:
                filas = list(csv.DictReader(f))
        except Exception:
            return None

        candidatos = []
        for fila in filas:
            if not fila.get("fecha_hora_envio", "").startswith(hoy_str):
                continue
            if fila.get("deporte") != deporte:
                continue
            try:
                hora_inicio = ZONA_HORARIA.localize(
                    datetime.strptime(fila["hora_inicio_evento"], "%Y-%m-%d %H:%M")
                )
            except Exception:
                continue
            if hora_inicio <= ahora:
                continue  # el partido de esa fila ya empezo, no sirve para parlay
            candidatos.append(fila)

        if not candidatos:
            return None
        return candidatos[-1]  # el mas reciente registrado hoy

    def evaluar_parlay_del_dia(self):
        """
        Combina el pick de FUTBOL y el de NBA de HOY (si ambos existen y
        ambos partidos siguen sin empezar). Calcula la probabilidad
        combinada real (multiplicando las probabilidades justas,
        asumiendo independencia -- son deportes y partidos distintos) y
        la cuota combinada estimada (multiplicando las mejores cuotas).
        Solo avisa si el edge combinado sigue por encima de
        EDGE_MINIMO_PARLAY. Si no hay 2 patas disponibles o no conviene
        combinarlas, tambien lo dice claramente -- nunca fuerza un parlay.
        """
        pierna_futbol = self.leer_pick_de_hoy("FUTBOL")
        pierna_nba = self.leer_pick_de_hoy("NBA")

        if not pierna_futbol or not pierna_nba:
            faltantes = []
            if not pierna_futbol:
                faltantes.append("Futbol")
            if not pierna_nba:
                faltantes.append("NBA")
            self.send_msg(
                f"📭 *PARLAY*: hoy no hay pick individual disponible de "
                f"{' y '.join(faltantes)}, asi que no hay 2 patas que combinar. "
                f"Sin parlay hoy — es preferible eso a forzar una combinacion sin base."
            )
            return

        try:
            prob1 = float(pierna_futbol["prob_justa_pct"]) / 100
            cuota1 = float(pierna_futbol["cuota"])
            prob2 = float(pierna_nba["prob_justa_pct"]) / 100
            cuota2 = float(pierna_nba["cuota"])
        except (KeyError, ValueError):
            return

        # Probabilidad combinada real (asumiendo independencia entre los
        # dos eventos -- razonable porque son deportes y partidos distintos)
        prob_combinada = prob1 * prob2

        # Cuota combinada ESTIMADA por multiplicacion simple. Aviso real:
        # muchas casas aplican un margen extra en parlays por encima de
        # esto, asi que la cuota que te ofrezca tu casa puede ser un poco
        # menor -- este numero es un techo, no una promesa.
        cuota_combinada_estimada = cuota1 * cuota2

        edge_parlay = (cuota_combinada_estimada * prob_combinada) - 1

        mensaje_base = (
            f"🎲 *PARLAY DE 2 PATAS - ANALISIS DE HOY*\n\n"
            f"1️⃣ ⚽ {pierna_futbol['evento']} — {pierna_futbol['resultado']} @ {cuota1:.2f}\n"
            f"2️⃣ 🏀 {pierna_nba['evento']} — {pierna_nba['resultado']} @ {cuota2:.2f}\n\n"
            f"📐 Probabilidad combinada real (ambas patas): {prob_combinada*100:.1f}%\n"
            f"🏦 Cuota combinada estimada: {cuota_combinada_estimada:.2f} "
            f"(techo — tu casa puede pagar un poco menos por margen de parlay)\n"
        )

        if edge_parlay >= EDGE_MINIMO_PARLAY:
            monto_parlay = self.bankroll * MAX_PORCENTAJE_BANKROLL  # tope fijo, sin Kelly compuesto aqui
            mensaje = mensaje_base + (
                f"📈 Edge estimado del parlay: {edge_parlay*100:.1f}%\n"
                f"💰 Si decides jugarlo, no mas de {MAX_PORCENTAJE_BANKROLL*100:.0f}% del bankroll "
                f"≈ ${monto_parlay:.2f}\n\n"
                f"⚠️ IMPORTANTE: esto es una ALTERNATIVA a jugar las 2 patas por separado, "
                f"no una apuesta adicional. Si ya apostaste alguna de las 2 individualmente, "
                f"NO la vuelvas a meter aqui — estarias arriesgando el doble en el mismo resultado.\n"
                f"La varianza de un parlay es mucho mayor que la de picks individuales, "
                f"aun cuando el edge calculado sea positivo."
            )
            self.registrar_parlay_csv(pierna_futbol, pierna_nba, cuota1, cuota2, prob_combinada, cuota_combinada_estimada, edge_parlay)
            self.actualizar_json_parlay_web()
        else:
            mensaje = mensaje_base + (
                f"📈 Edge estimado del parlay: {edge_parlay*100:.1f}% (por debajo del "
                f"{EDGE_MINIMO_PARLAY*100:.0f}% minimo)\n\n"
                f"🙅 No conviene combinarlas hoy. Cada pata individual ya tenia valor por "
                f"separado — quedate con esas por separado en vez de combinarlas."
            )

        self.send_msg(mensaje)

    def registrar_parlay_csv(self, pierna1, pierna2, cuota1, cuota2, prob_combinada, cuota_combinada, edge):
        archivo_existe = os.path.isfile(PARLAY_CSV)
        try:
            with open(PARLAY_CSV, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not archivo_existe:
                    writer.writerow(PARLAY_HEADERS)
                writer.writerow([
                    datetime.now(ZONA_HORARIA).strftime("%Y-%m-%d %H:%M:%S"),
                    pierna1["evento"], pierna1["resultado"], f"{cuota1:.2f}",
                    pierna2["evento"], pierna2["resultado"], f"{cuota2:.2f}",
                    f"{prob_combinada*100:.1f}", f"{cuota_combinada:.2f}", f"{edge*100:.1f}",
                    "", "",  # resultado_real, ganancia_perdida -- los llenas tu
                ])
        except Exception as e:
            print(f"Error escribiendo parlay en CSV: {e}")

    def actualizar_json_parlay_web(self):
        if not os.path.isfile(PARLAY_CSV):
            return
        try:
            with open(PARLAY_CSV, mode="r", encoding="utf-8") as f:
                filas = list(csv.DictReader(f))
            os.makedirs(os.path.dirname(PARLAY_JSON), exist_ok=True)
            with open(PARLAY_JSON, mode="w", encoding="utf-8") as f:
                json.dump(filas, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error actualizando JSON de parlays: {e}")

    # ------------------------------------------------------------------
    # Programacion horaria
    # ------------------------------------------------------------------
    def verificar_horarios(self):
        ahora = datetime.now(ZONA_HORARIA).strftime("%H:%M")

        if ahora == "00:00":
            self.alarmas_enviadas = {d: False for d in DEPORTES}
            self.parlay_enviado_hoy = False

        for deporte, config in DEPORTES.items():
            if ahora == config["hora"] and not self.alarmas_enviadas[deporte]:
                self.escanear_deporte(deporte)
                self.alarmas_enviadas[deporte] = True

        # Evalua el parlay 10 minutos despues del ultimo escaneo del dia (NBA)
        if ahora == "16:10" and not getattr(self, "parlay_enviado_hoy", False):
            self.evaluar_parlay_del_dia()
            self.parlay_enviado_hoy = True

    def ejecutar_una_vez(self, deporte=None):
        """
        Modo para ejecucion programada (ej: GitHub Actions cron).
        Si se especifica 'deporte', escanea solo ese (o evalua el parlay
        si el valor es 'PARLAY'). Si no se especifica nada, revisa todos
        los horarios que correspondan a la hora actual.
        """
        self.revisar_comandos()
        if deporte:
            deporte = deporte.upper()
            if deporte == "PARLAY":
                self.evaluar_parlay_del_dia()
            elif deporte in DEPORTES:
                self.escanear_deporte(deporte)
            else:
                print(f"Deporte desconocido: {deporte}")
        else:
            self.verificar_horarios()

    def loop(self):
        requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates?offset=-1")
        print("🚀 Nexus Sports Value Scanner Online.")
        self.send_msg(
            "🛡️ *NEXUS VALUE SCANNER activo*\n"
            "Compara cuotas reales entre casas y solo avisa si hay valor estadistico genuino.\n"
            "Si un deporte no tiene edge ese dia, no se envia nada — es lo esperado, no un error.\n\n"
            "Horarios:\n"
            "⚽ 07:30 Futbol\n🎾 08:00 Tenis\n🏒 15:30 Hockey\n🏀 16:00 NBA\n🎲 16:10 Analisis de parlay (Futbol+NBA, si aplica)\n\n"
            "Comandos: STATUS (estado actual) | LOG (resumen de tu historial de picks)"
        )
        while True:
            self.revisar_comandos()
            self.verificar_horarios()
            time.sleep(30)


if __name__ == "__main__":
    import sys
    if not TOKEN_TG or not API_KEY_ODDS or not CHAT_ID:
        print("ERROR: faltan variables de entorno TOKEN_TG, API_KEY_ODDS o CHAT_ID.")
        sys.exit(1)

    # Uso:
    #   python nexus_sports_value.py            -> modo loop (24/7, para PC/VPS propio)
    #   python nexus_sports_value.py once        -> corre una vez, revisa el horario actual
    #   python nexus_sports_value.py once FUTBOL -> corre una vez, fuerza el escaneo de ese deporte
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        deporte_arg = sys.argv[2] if len(sys.argv) > 2 else None
        NexusSportsValue().ejecutar_una_vez(deporte_arg)
    else:
        NexusSportsValue().loop()
