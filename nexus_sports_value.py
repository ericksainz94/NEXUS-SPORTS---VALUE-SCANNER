import requests
import time
import csv
import json
import os
from datetime import datetime
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

DEPORTES = {
    "FUTBOL":  {"sport_key": "soccer_epl",       "market": "totals",  "hora": "07:30", "emoji": "⚽"},
    "TENIS":   {"sport_key": "tennis_atp",        "market": "h2h",      "hora": "08:00", "emoji": "🎾"},
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

    def calcular_probabilidad_justa_consenso(self, outcome_name, bookmakers, market_key):
        """
        Junta las cuotas de un mismo resultado (ej: 'Over 2.5') en TODAS las
        casas disponibles, quita el vig de cada casa por separado, y
        promedia esas probabilidades "justas" entre casas para obtener un
        consenso de mercado. Esto es mas robusto que confiar en una sola casa.
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
                    if o.get("name") == outcome_name or str(o.get("point")) == str(outcome_name):
                        probabilidades_justas.append(p_justa)

        if not probabilidades_justas:
            return None
        return sum(probabilidades_justas) / len(probabilidades_justas)

    def mejor_cuota_disponible(self, outcome_name, bookmakers, market_key):
        mejor = None
        casa_mejor = None
        for bookmaker in bookmakers:
            for market in bookmaker.get("markets", []):
                if market.get("key") != market_key:
                    continue
                for o in market.get("outcomes", []):
                    if o.get("name") == outcome_name or str(o.get("point")) == str(outcome_name):
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

            bookmakers = evento.get("bookmakers", [])
            if len(bookmakers) < 2:
                continue  # necesitamos al menos 2 casas para comparar consenso

            equipo_local = evento.get('home_team', 'Local')
            equipo_visita = evento.get('away_team', 'Visita')

            # Juntamos todos los nombres de resultado distintos que aparecen
            nombres_resultado = set()
            for bookmaker in bookmakers:
                for market in bookmaker.get("markets", []):
                    if market.get("key") != market_key:
                        continue
                    for o in market.get("outcomes", []):
                        nombres_resultado.add(o.get("name") if market_key != "totals" else str(o.get("point")))

            for nombre_resultado in nombres_resultado:
                if nombre_resultado is None:
                    continue
                prob_justa = self.calcular_probabilidad_justa_consenso(nombre_resultado, bookmakers, market_key)
                if prob_justa is None:
                    continue
                mejor_cuota, casa = self.mejor_cuota_disponible(nombre_resultado, bookmakers, market_key)
                if mejor_cuota is None:
                    continue

                # Valor esperado de apostar 1 unidad a la mejor cuota disponible
                ev = (mejor_cuota * prob_justa) - 1

                if ev >= EDGE_MINIMO:
                    fraccion_bankroll = self.kelly_fraccionado(prob_justa, mejor_cuota)
                    if fraccion_bankroll <= 0:
                        continue
                    mejores_picks.append({
                        "evento": f"{equipo_local} vs {equipo_visita}",
                        "resultado": nombre_resultado,
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
            return

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

    # ------------------------------------------------------------------
    # Programacion horaria
    # ------------------------------------------------------------------
    def verificar_horarios(self):
        ahora = datetime.now(ZONA_HORARIA).strftime("%H:%M")

        if ahora == "00:00":
            self.alarmas_enviadas = {d: False for d in DEPORTES}

        for deporte, config in DEPORTES.items():
            if ahora == config["hora"] and not self.alarmas_enviadas[deporte]:
                self.escanear_deporte(deporte)
                self.alarmas_enviadas[deporte] = True

    def ejecutar_una_vez(self, deporte=None):
        """
        Modo para ejecucion programada (ej: GitHub Actions cron).
        Si se especifica 'deporte', escanea solo ese. Si no, escanea todos
        los que correspondan a la hora actual (util si corres el workflow
        una vez al dia y quieres que revise el horario el mismo).
        """
        self.revisar_comandos()
        if deporte:
            deporte = deporte.upper()
            if deporte in DEPORTES:
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
            "⚽ 07:30 Futbol\n🎾 08:00 Tenis\n🏒 15:30 Hockey\n🏀 16:00 NBA\n\n"
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
