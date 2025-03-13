import ccxt
import pandas as pd
import numpy as np
import time
import datetime
import threading
import configparser
import os
import json
import uuid
import pickle
import logging
from flask import Flask, render_template, request, jsonify, flash, redirect, url_for, session
from flask_socketio import SocketIO, emit

# Configurar logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = "tu_clave_secreta_aqui"  # Cambiar por una clave segura
socketio = SocketIO(app)

# Funciones utilitarias para conversiones seguras
def safe_float(value, default=0.0):
    """Convierte un valor a float de forma segura"""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default

def safe_int(value, default=1):
    """Convierte un valor a int de forma segura"""
    if value is None:
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default

# Añade esta función en la parte superior del archivo trading_bot.py, justo después de las importaciones
def datetime_to_string(obj):
    """Convierte objetos datetime a string para serialización JSON"""
    if isinstance(obj, datetime.datetime):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    return obj

class BotInstance:
    """Clase para cada instancia individual del bot"""

    def __init__(self, instance_id, exchange, symbol, timeframe, market_type, strategy_name, strategy_config,
                 investment_amount, custom_strategy=None):
        self.instance_id = instance_id
        self.exchange = exchange
        self.symbol = symbol
        self.timeframe = timeframe
        self.market_type = market_type
        self.strategy_name = strategy_name
        self.investment_amount = investment_amount
        self.custom_strategy = custom_strategy

        # Configuración de la estrategia
        for key, value in strategy_config.items():
            setattr(self, key, value)

        # Estado del bot
        self.is_running = False
        self.thread = None
        self.current_position = None
        self.trade_history = []
        self.last_update_time = None
        self.status = "Configurado"
        self.profit_loss = 0.0

    def start(self):
        if not self.is_running:
            self.is_running = True
            self.status = "Activo"
            self.thread = threading.Thread(target=self.trading_loop)
            self.thread.daemon = True
            self.thread.start()
            return True
        return False

    def stop(self, close_positions=False):
        if self.is_running:
            if close_positions and self.current_position:
                self.close_position()
            self.is_running = False
            self.status = "Detenido"
            return True
        return False

    def close_position(self):
        """Cerrar la posición actual"""
        if not self.current_position:
            return None

        try:
            # Obtener precio actual
            ticker = self.exchange.fetch_ticker(self.symbol)
            current_price = ticker['last']

            # Para OKX, usamos el método específico para cerrar posiciones
            if self.exchange.id == 'okx' and self.market_type == "future":
                # Configurar el tipo de mercado
                self.exchange.options['defaultType'] = 'swap'

                # Determinar el lado de la posición actual
                posSide = "long" if self.current_position["side"] == "long" else "short"

                # Intentar cerrar usando closePosition
                try:
                    # Crear la petición de cierre
                    self.exchange.privatePostTradeClosePosition({
                        'instId': self.symbol,
                        'mgnMode': 'cross',
                        'posSide': posSide
                    })

                    logger.info(f"[{self.symbol}] Posición cerrada usando closePosition para {posSide}")
                except Exception as e:
                    logger.error(f"Error en closePosition: {str(e)}")

                    # Si falla, intentamos con el método alternativo
                    side = "sell" if self.current_position["side"] == "long" else "buy"

                    # Para OKX, necesitamos usar reduceOnly en lugar de closePosition
                    order = self.exchange.create_order(
                        symbol=self.symbol,
                        type='market',
                        side=side,
                        amount=self.current_position["amount"],
                        params={
                            'tdMode': 'cross',
                            'reduceOnly': True  # Esto es clave para cerrar en lugar de abrir una contraria
                        }
                    )

                    logger.info(f"[{self.symbol}] Posición cerrada usando reduceOnly para {side}")
            else:
                # Para otros exchanges, usamos el método normal
                side = "sell" if self.current_position["side"] == "long" else "buy"

                # Parámetros para el cierre
                order_params = {}

                # Configuración según tipo de mercado
                if self.market_type == "future":
                    if self.exchange.id == 'binance':
                        self.exchange.options['defaultType'] = 'future'
                        order_params['reduceOnly'] = True
                else:
                    self.exchange.options['defaultType'] = 'spot'

                # Crear orden de cierre
                order = self.exchange.create_order(
                    symbol=self.symbol,
                    type='market',
                    side=side,
                    amount=self.current_position["amount"],
                    params=order_params
                )

            # Calcular P&L
            if self.current_position["side"] == "long":
                pnl = (current_price - self.current_position["entry_price"]) / self.current_position[
                    "entry_price"] * 100
            else:
                pnl = (self.current_position["entry_price"] - current_price) / self.current_position[
                    "entry_price"] * 100

            # Crear registro de operación
            trade_record = {
                "fecha": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "tipo": f"Cierre {self.current_position['side']}",
                "precio_entrada": self.current_position["entry_price"],
                "precio_salida": current_price,
                "cantidad": self.current_position["amount"],
                "pnl": f"{pnl:.2f}%"
            }

            # Añadir al historial
            self.trade_history.append(trade_record)

            # Actualizar P&L total
            self.profit_loss += pnl

            # Limpiar posición
            self.current_position = None

            return trade_record

        except Exception as e:
            logger.error(f"Error al cerrar posición: {str(e)}")
            return None

    def trading_loop(self):
        """Bucle principal de trading"""
        last_check_time = 0

        while self.is_running:
            try:
                # Actualizar tiempo de la última comprobación
                self.last_update_time = datetime.datetime.now()

                # Limitar frecuencia de verificación
                current_time = time.time()
                if current_time - last_check_time < 5:  # Mínimo 5 segundos entre verificaciones
                    time.sleep(1)
                    continue

                last_check_time = current_time

                # Obtener datos OHLCV
                ohlcv = self.exchange.fetch_ohlcv(self.symbol, self.timeframe, limit=100)
                if not ohlcv or len(ohlcv) < 30:  # Necesitamos suficientes datos
                    time.sleep(5)
                    continue

                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

                # Calcular indicadores
                df = self.calculate_indicators(df)

                # Generar señales
                if self.custom_strategy:
                    # Usar estrategia personalizada si existe
                    signal = self.custom_strategy(df, self)
                else:
                    # Usar estrategia predefinida
                    signal = self.generate_signal(df)

                # Ejecutar señales
                if signal:
                    self.execute_signal(signal, df)

                # Gestionar posiciones abiertas
                self.manage_open_positions(df)

                # Esperar antes del siguiente ciclo
                time.sleep(5)

            except Exception as e:
                logger.error(f"Error en bucle de trading para {self.symbol}: {str(e)}")
                time.sleep(10)

    def calculate_indicators(self, df):
        """Calcular indicadores técnicos"""
        # EMAs
        df['ema_fast'] = df['close'].ewm(span=self.ema_fast_period).mean()
        df['ema_slow'] = df['close'].ewm(span=self.ema_slow_period).mean()

        # RSI
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(window=self.rsi_period).mean()
        loss = -delta.where(delta < 0, 0).rolling(window=self.rsi_period).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))

        # MACD
        ema_fast = df['close'].ewm(span=self.macd_fast).mean()
        ema_slow = df['close'].ewm(span=self.macd_slow).mean()
        df['macd'] = ema_fast - ema_slow
        df['macd_signal'] = df['macd'].ewm(span=self.macd_signal).mean()
        df['macd_hist'] = df['macd'] - df['macd_signal']

        # Bandas de Bollinger
        df['ma'] = df['close'].rolling(window=self.bb_period).mean()
        df['std'] = df['close'].rolling(window=self.bb_period).std()
        df['bb_upper'] = df['ma'] + (df['std'] * self.bb_std)
        df['bb_lower'] = df['ma'] - (df['std'] * self.bb_std)

        # ATR
        tr1 = df['high'] - df['low']
        tr2 = abs(df['high'] - df['close'].shift())
        tr3 = abs(df['low'] - df['close'].shift())
        df['tr'] = pd.DataFrame([tr1, tr2, tr3]).max()
        df['atr'] = df['tr'].rolling(window=self.atr_period).mean()

        return df

    def generate_signal(self, df):
        """Generar señales de trading basadas en la estrategia configurada"""
        # Obtener últimas filas para análisis
        if len(df) < 2:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # Verificar si ya tenemos una posición abierta
        if self.current_position:
            # Señales para cerrar posición
            if self.current_position['side'] == 'long':
                # Cerrar largo
                if (last['close'] < last['ema_slow'] and prev['close'] >= prev['ema_slow']) or \
                        (last['rsi'] > self.rsi_overbought and prev['rsi'] <= self.rsi_overbought) or \
                        (last['macd'] < last['macd_signal'] and prev['macd'] >= prev['macd_signal']):
                    return {"action": "close", "reason": "señal técnica"}
            else:
                # Cerrar corto
                if (last['close'] > last['ema_slow'] and prev['close'] <= prev['ema_slow']) or \
                        (last['rsi'] < self.rsi_oversold and prev['rsi'] >= self.rsi_oversold) or \
                        (last['macd'] > last['macd_signal'] and prev['macd'] <= prev['macd_signal']):
                    return {"action": "close", "reason": "señal técnica"}
        else:
            # Señales para nuevas posiciones

            # Señal de compra (largo)
            if last['close'] > last['ema_slow'] and \
                    last['macd'] > last['macd_signal'] and \
                    last['rsi'] < self.rsi_overbought and \
                    last['rsi'] > 50:
                return {"action": "buy", "price": last['close'], "atr": last['atr']}

            # Señal de venta (corto) - solo en futuros
            if self.market_type == "future" and \
                    last['close'] < last['ema_slow'] and \
                    last['macd'] < last['macd_signal'] and \
                    last['rsi'] > self.rsi_oversold and \
                    last['rsi'] < 50:
                return {"action": "sell", "price": last['close'], "atr": last['atr']}

        return None

    def execute_signal(self, signal, df):
        """Ejecutar señales de trading"""
        try:
            if signal["action"] == "close" and self.current_position:
                # Cerrar posición
                trade_record = self.close_position()
                if trade_record:
                    logger.info(
                        f"[{self.symbol}] Posición cerrada: {trade_record['tipo']} a {trade_record['precio_salida']}, P&L: {trade_record['pnl']}")

            elif signal["action"] in ["buy", "sell"] and not self.current_position:
                # Abrir nueva posición
                entry_price = signal.get("price")
                atr = signal.get("atr")

                # Verificar que entry_price y atr tengan valores válidos
                if entry_price is None or atr is None:
                    logger.error(f"[{self.symbol}] Valores inválidos: precio={entry_price}, atr={atr}")
                    return

                # Usar conversiones seguras para asegurar valores numéricos
                entry_price = safe_float(entry_price)
                atr = safe_float(atr)

                if entry_price <= 0 or atr <= 0:
                    logger.error(f"[{self.symbol}] Valores numéricos inválidos: precio={entry_price}, atr={atr}")
                    return

                # Asegurar que todos los multiplicadores sean valores numéricos
                atr_multiplier = safe_float(getattr(self, 'atr_multiplier', 2.0))
                take_profit_pct = safe_float(getattr(self, 'take_profit_pct', 3.0))

                # Calcular stop loss
                stop_loss = entry_price - (atr * atr_multiplier) if signal["action"] == "buy" else \
                    entry_price + (atr * atr_multiplier)

                # Calcular take profit
                take_profit = entry_price * (1 + take_profit_pct / 100) if signal["action"] == "buy" else \
                    entry_price * (1 - take_profit_pct / 100)

                # Calcular tamaño de posición basado en el monto de inversión
                investment_amount = safe_float(self.investment_amount, 10.0)  # Valor predeterminado seguro

                if signal["action"] == "buy":
                    # Para posiciones largas, calcular cantidad en tokens
                    amount = investment_amount / entry_price
                else:
                    # Para posiciones cortas, usar el valor en USDT directamente
                    amount = investment_amount / entry_price

                # Si es futuros, considerar apalancamiento con protección contra valores nulos
                if self.market_type == "future" and hasattr(self, 'leverage'):
                    # Usar conversión segura para leverage
                    leverage = safe_float(self.leverage, 1.0)
                    if leverage > 0:
                        amount = amount * leverage
                        logger.info(
                            f"[{self.symbol}] Apalancamiento aplicado: {leverage}x, cantidad ajustada: {amount}")
                    else:
                        logger.warning(f"[{self.symbol}] Valor de apalancamiento inválido: {self.leverage}, usando 1x")

                # Redondear según precisión del exchange
                try:
                    original_amount = amount
                    amount = float(self.exchange.amount_to_precision(self.symbol, amount))
                    logger.info(f"[{self.symbol}] Cantidad redondeada: {original_amount} → {amount}")
                except Exception as e:
                    logger.warning(
                        f"[{self.symbol}] Error al redondear cantidad: {str(e)}. Usando cantidad aproximada.")
                    amount = round(amount, 8)

                # Verificar mínimo
                try:
                    market = self.exchange.markets[self.symbol]
                    min_amount = safe_float(market.get('limits', {}).get('amount', {}).get('min', 0), 0)
                except Exception as e:
                    logger.warning(f"[{self.symbol}] Error al obtener cantidad mínima: {str(e)}. Asumiendo 0.")
                    min_amount = 0

                if amount < min_amount and min_amount > 0:
                    logger.warning(f"[{self.symbol}] Cantidad calculada ({amount}) menor que el mínimo ({min_amount})")
                    return

                # Crear orden con configuración específica del tipo de mercado
                try:
                    # Configuración según el tipo de mercado
                    if self.market_type == "future":
                        # MODIFICACIÓN CRÍTICA: Para OKX, usamos 'swap' en lugar de 'future'
                        if self.exchange.id == 'okx':
                            self.exchange.options['defaultType'] = 'swap'
                        else:
                            self.exchange.options['defaultType'] = 'future'

                        # Configurar apalancamiento primero
                        if hasattr(self, 'leverage') and self.leverage is not None:
                            try:
                                leverage_int = safe_int(self.leverage, 1)

                                if self.exchange.id == 'binance':
                                    self.exchange.fapiPrivatePostLeverage({
                                        'symbol': self.symbol.replace('/', ''),
                                        'leverage': leverage_int
                                    })
                                    logger.info(
                                        f"[{self.symbol}] Configurado apalancamiento en Binance: {leverage_int}x")
                                elif self.exchange.id == 'okx':
                                    # Para OKX, simplemente configuramos el apalancamiento sin parámetros adicionales
                                    try:
                                        self.exchange.set_leverage(leverage_int, self.symbol)
                                        logger.info(
                                            f"[{self.symbol}] Configurado apalancamiento en OKX: {leverage_int}x")
                                    except Exception as e:
                                        logger.warning(
                                            f"[{self.symbol}] No se pudo configurar apalancamiento en OKX: {str(e)}")
                            except Exception as e:
                                logger.warning(f"[{self.symbol}] No se pudo configurar apalancamiento: {str(e)}")
                    else:
                        self.exchange.options['defaultType'] = 'spot'

                    # Parámetros para la orden
                    order_params = {}

                    # Agregar parámetros específicos según el exchange
                    if self.exchange.id == 'okx' and self.market_type == "future":
                        # Para OKX solo agregamos el modo de margen
                        order_params['tdMode'] = 'cross'
                        # No agregamos posSide que es lo que causa problemas

                    logger.info(
                        f"[{self.symbol}] Creando orden: {signal['action']} {amount} {self.symbol} a {entry_price} con parámetros: {order_params}")

                    # Crear la orden
                    order = self.exchange.create_order(
                        symbol=self.symbol,
                        type='market',
                        side=signal["action"],
                        amount=amount,
                        params=order_params
                    )

                    # Registrar posición
                    self.current_position = {
                        "side": "long" if signal["action"] == "buy" else "short",
                        "entry_price": entry_price,
                        "amount": amount,
                        "stop_loss": stop_loss,
                        "take_profit": take_profit,
                        "entry_time": datetime.datetime.now(),
                        "current_price": entry_price
                    }

                    # Añadir al historial
                    trade_record = {
                        "fecha": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "tipo": f"Apertura {self.current_position['side']}",
                        "precio_entrada": entry_price,
                        "precio_salida": "-",
                        "cantidad": amount,
                        "pnl": "-"
                    }

                    self.trade_history.append(trade_record)

                    logger.info(
                        f"[{self.symbol}] Posición abierta: {signal['action']} {amount} a {entry_price} en mercado {self.market_type}")

                except Exception as e:
                    logger.error(f"[{self.symbol}] Error al crear orden: {str(e)}")

        except Exception as e:
            logger.error(f"[{self.symbol}] Error al ejecutar señal: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())

    def manage_open_positions(self, df):
        """Gestionar posiciones abiertas (trailing stop, take profit)"""
        if not self.current_position:
            return

        try:
            # Obtener precio actual
            ticker = self.exchange.fetch_ticker(self.symbol)
            current_price = ticker['last']
            self.current_position['current_price'] = current_price

            # Verificar stop loss
            if (self.current_position['side'] == 'long' and current_price <= self.current_position['stop_loss']) or \
                    (self.current_position['side'] == 'short' and current_price >= self.current_position['stop_loss']):
                # Cerrar por stop loss
                signal = {"action": "close", "reason": "stop loss"}
                self.execute_signal(signal, df)
                return

            # Verificar take profit
            if (self.current_position['side'] == 'long' and current_price >= self.current_position['take_profit']) or \
                    (self.current_position['side'] == 'short' and current_price <= self.current_position[
                        'take_profit']):
                # Cerrar por take profit
                signal = {"action": "close", "reason": "take profit"}
                self.execute_signal(signal, df)
                return

            # Trailing stop
            if self.current_position['side'] == 'long':
                # Calcular nuevo stop loss para largo
                trailing_distance = current_price * (self.trailing_stop_pct / 100)
                new_stop_loss = current_price - trailing_distance

                # Actualizar solo si es mayor que el actual
                if new_stop_loss > self.current_position['stop_loss']:
                    self.current_position['stop_loss'] = new_stop_loss

            elif self.current_position['side'] == 'short':
                # Calcular nuevo stop loss para corto
                trailing_distance = current_price * (self.trailing_stop_pct / 100)
                new_stop_loss = current_price + trailing_distance

                # Actualizar solo si es menor que el actual
                if new_stop_loss < self.current_position['stop_loss']:
                    self.current_position['stop_loss'] = new_stop_loss

        except Exception as e:
            logger.error(f"[{self.symbol}] Error al gestionar posición: {str(e)}")


class TradingBotManager:
    def __init__(self):
        # Lista de instancias de bots
        self.bot_instances = {}

        # Conexiones a exchanges
        self.exchanges = {}

        # Variables para la configuración
        self.exchange_configs = {}

        # Estrategias personalizadas registradas
        self.custom_strategies = {}

        # Perfiles de estrategia predefinidos
        self.strategy_profiles = {
            "Conservadora (Spot)": {
                "description": "Estrategia de bajo riesgo para mercado spot, enfocada en tendencias a largo plazo",
                "market_type": "spot",
                "timeframe": "4h",
                "risk_percentage": 1.0,
                "take_profit_pct": 3.0,
                "ema_fast_period": 20,
                "ema_slow_period": 50,
                "rsi_period": 14,
                "rsi_overbought": 70,
                "rsi_oversold": 30,
                "macd_fast": 12,
                "macd_slow": 26,
                "macd_signal": 9,
                "bb_period": 20,
                "bb_std": 2,
                "atr_period": 14,
                "atr_multiplier": 2.0,
                "trailing_stop_pct": 1.5
            },
            "Agresiva (Spot)": {
                "description": "Estrategia de mayor riesgo para mercado spot, busca movimientos de corto plazo",
                "market_type": "spot",
                "timeframe": "15m",
                "risk_percentage": 2.5,
                "take_profit_pct": 5.0,
                "ema_fast_period": 10,
                "ema_slow_period": 30,
                "rsi_period": 8,
                "rsi_overbought": 75,
                "rsi_oversold": 25,
                "macd_fast": 6,
                "macd_slow": 19,
                "macd_signal": 6,
                "bb_period": 15,
                "bb_std": 2.5,
                "atr_period": 10,
                "atr_multiplier": 1.5,
                "trailing_stop_pct": 2.0
            },
            "Conservadora (Futuros)": {
                "description": "Estrategia de bajo riesgo para mercado de futuros con bajo apalancamiento",
                "market_type": "future",
                "timeframe": "1h",
                "leverage": 2,
                "risk_percentage": 1.0,
                "take_profit_pct": 2.5,
                "ema_fast_period": 25,
                "ema_slow_period": 75,
                "rsi_period": 14,
                "rsi_overbought": 65,
                "rsi_oversold": 35,
                "macd_fast": 12,
                "macd_slow": 26,
                "macd_signal": 9,
                "bb_period": 20,
                "bb_std": 2,
                "atr_period": 14,
                "atr_multiplier": 2.0,
                "trailing_stop_pct": 1.0
            },
            "Agresiva (Futuros)": {
                "description": "Estrategia de alto riesgo para mercado de futuros con apalancamiento moderado",
                "market_type": "future",
                "timeframe": "5m",
                "leverage": 5,
                "risk_percentage": 2.0,
                "take_profit_pct": 4.0,
                "ema_fast_period": 8,
                "ema_slow_period": 21,
                "rsi_period": 7,
                "rsi_overbought": 75,
                "rsi_oversold": 25,
                "macd_fast": 6,
                "macd_slow": 14,
                "macd_signal": 4,
                "bb_period": 12,
                "bb_std": 2.5,
                "atr_period": 7,
                "atr_multiplier": 1.2,
                "trailing_stop_pct": 2.5
            }
        }

        # Contador para el hilo de monitoreo
        self.monitor_thread = None
        self.is_monitoring = False

        # Cargar configuración guardada
        self.load_config()

        # Iniciar monitoreo
        self.start_monitoring()

    def register_custom_strategy(self, name, strategy_func, description="Estrategia personalizada"):
        """Registra una estrategia personalizada"""
        self.custom_strategies[name] = {
            "function": strategy_func,
            "description": description
        }
        logger.info(f"Estrategia personalizada '{name}' registrada")
        return True

    def get_custom_strategies(self):
        """Devuelve las estrategias personalizadas registradas"""
        return {name: data["description"] for name, data in self.custom_strategies.items()}

    def add_bot(self, exchange_name, symbol, timeframe, market_type, strategy_name,
                investment_amount, strategy_config=None, custom_strategy_name=None):
        """Añade un nuevo bot a la gestión"""
        try:
            # Verificar que el exchange esté configurado
            if exchange_name not in self.exchange_configs:
                return False, "Exchange no configurado"

            # Conectar al exchange si no está conectado
            if exchange_name not in self.exchanges:
                try:
                    self.connect_exchange(exchange_name)
                except Exception as e:
                    return False, f"Error al conectar con el exchange: {str(e)}"

            exchange = self.exchanges[exchange_name]

            # Verificar que el par existe
            try:
                exchange.load_markets()
                if symbol not in exchange.markets:
                    return False, f"El par {symbol} no está disponible en {exchange_name}"
            except Exception as e:
                return False, f"Error al cargar mercados: {str(e)}"

            # Obtener configuración de estrategia
            if custom_strategy_name:
                if custom_strategy_name not in self.custom_strategies:
                    return False, f"Estrategia personalizada '{custom_strategy_name}' no encontrada"

                strategy_config = strategy_config or {}  # Usar config proporcionada o crear una vacía
                custom_strategy_func = self.custom_strategies[custom_strategy_name]["function"]
            else:
                if strategy_name not in self.strategy_profiles:
                    return False, f"Estrategia predefinida '{strategy_name}' no encontrada"

                # Usar la configuración predefinida de la estrategia
                strategy_config = strategy_config or self.strategy_profiles[strategy_name].copy()
                custom_strategy_func = None

            # Crear ID único para el bot
            instance_id = str(uuid.uuid4())[:8]

            # Crear instancia de bot
            bot_instance = BotInstance(
                instance_id=instance_id,
                exchange=exchange,
                symbol=symbol,
                timeframe=timeframe or strategy_config.get('timeframe', '1h'),
                market_type=market_type,
                strategy_name=strategy_name if not custom_strategy_name else custom_strategy_name,
                strategy_config=strategy_config,
                investment_amount=investment_amount,
                custom_strategy=custom_strategy_func if custom_strategy_name else None
            )

            # Añadir a la lista de bots
            self.bot_instances[instance_id] = bot_instance

            # Guardar configuración
            self.save_config()

            return True, instance_id
        except Exception as e:
            logger.error(f"Error al añadir bot: {str(e)}")
            return False, f"Error al crear instancia de bot: {str(e)}"

    def remove_bot(self, bot_id):
        """Elimina un bot de la gestión"""
        if bot_id not in self.bot_instances:
            return False, "Bot no encontrado"

        bot = self.bot_instances[bot_id]

        # Si está en ejecución, detenerlo
        if bot.is_running:
            bot.stop(close_positions=True)

        # Eliminar bot
        del self.bot_instances[bot_id]

        # Guardar configuración
        self.save_config()

        return True, "Bot eliminado correctamente"

    def start_bot(self, bot_id):
        """Inicia un bot específico"""
        if bot_id not in self.bot_instances:
            return False, "Bot no encontrado"

        bot = self.bot_instances[bot_id]

        if bot.start():
            return True, "Bot iniciado correctamente"
        else:
            return False, "El bot ya está en ejecución"

    def stop_bot(self, bot_id, close_positions=False):
        """Detiene un bot específico"""
        if bot_id not in self.bot_instances:
            return False, "Bot no encontrado"

        bot = self.bot_instances[bot_id]

        if bot.stop(close_positions):
            return True, "Bot detenido correctamente"
        else:
            return False, "El bot no está en ejecución"

    def get_bot_status(self, bot_id):
        """Obtiene el estado de un bot específico"""
        if bot_id not in self.bot_instances:
            return None

        bot = self.bot_instances[bot_id]

        # Calcular P&L si hay posición abierta
        current_pnl = bot.profit_loss
        if bot.current_position and 'current_price' in bot.current_position:
            entry_price = bot.current_position['entry_price']
            current_price = bot.current_position['current_price']

            if bot.current_position['side'] == 'long':
                position_pnl = (current_price - entry_price) / entry_price * 100
            else:  # short
                position_pnl = (entry_price - current_price) / entry_price * 100

            current_pnl += position_pnl

        return {
            "id": bot.instance_id,
            "exchange": bot.exchange.id,
            "symbol": bot.symbol,
            "strategy": bot.strategy_name,
            "market_type": bot.market_type,
            "status": bot.status,
            "is_running": bot.is_running,
            "profit_loss": f"{current_pnl:.2f}%",
            "last_update": bot.last_update_time.strftime("%Y-%m-%d %H:%M:%S") if bot.last_update_time else "-",
            "has_position": bot.current_position is not None,
            "position": bot.current_position
        }

    def get_all_bots(self):
        """Obtiene la lista de todos los bots"""
        return {bot_id: self.get_bot_status(bot_id) for bot_id in self.bot_instances}

    def get_bot_history(self, bot_id):
        """Obtiene el historial de un bot específico"""
        if bot_id not in self.bot_instances:
            return []

        return self.bot_instances[bot_id].trade_history

    def close_bot_position(self, bot_id):
        """Cierra la posición de un bot específico"""
        if bot_id not in self.bot_instances:
            return False, "Bot no encontrado"

        bot = self.bot_instances[bot_id]

        if not bot.current_position:
            return False, "El bot no tiene posiciones abiertas"

        # Cerrar posición
        trade_record = bot.close_position()

        if trade_record:
            return True, f"Posición cerrada con P&L: {trade_record['pnl']}"
        else:
            return False, "No se pudo cerrar la posición"

    def connect_exchange(self, exchange_name):
        """Conecta a un exchange y lo almacena en el diccionario"""
        if exchange_name not in self.exchange_configs:
            raise ValueError(f"No se encontró la configuración para {exchange_name}")

        exchange_class = getattr(ccxt, exchange_name)
        exchange = exchange_class(self.exchange_configs[exchange_name])

        # Prueba de conexión
        exchange.fetch_balance()

        # Guardar exchange
        self.exchanges[exchange_name] = exchange

        return exchange

    def add_exchange(self, exchange_name, api_key, api_secret, password=None):
        """Añade un nuevo exchange a la configuración"""
        try:
            # Crear configuración
            config = {
                'apiKey': api_key,
                'secret': api_secret,
                'enableRateLimit': True
            }

            if password:
                config['password'] = password

            # Verificar que la configuración funciona
            exchange_class = getattr(ccxt, exchange_name)
            temp_exchange = exchange_class(config)
            temp_exchange.fetch_balance()  # Prueba de conexión

            # Guardar configuración
            self.exchange_configs[exchange_name] = config

            # Guardar en archivo
            self.save_config()

            return True, "Exchange añadido correctamente"
        except Exception as e:
            return False, f"Error al añadir exchange: {str(e)}"

    def remove_exchange(self, exchange_name):
        """Elimina un exchange de la configuración"""
        if exchange_name not in self.exchange_configs:
            return False, "Exchange no encontrado"

        # Verificar si hay bots usando este exchange
        bots_using_exchange = [bot_id for bot_id, bot in self.bot_instances.items()
                               if bot.exchange.id == exchange_name]

        if bots_using_exchange:
            return False, f"No se puede eliminar el exchange. Está siendo utilizado por {len(bots_using_exchange)} bot(s)"

        # Eliminar el exchange
        if exchange_name in self.exchange_configs:
            del self.exchange_configs[exchange_name]

        if exchange_name in self.exchanges:
            del self.exchanges[exchange_name]

        # Guardar configuración
        self.save_config()

        return True, "Exchange eliminado correctamente"

    def start_monitoring(self):
        """Inicia el hilo de monitoreo"""
        if not self.is_monitoring:
            self.is_monitoring = True
            self.monitor_thread = threading.Thread(target=self.monitoring_loop)
            self.monitor_thread.daemon = True
            self.monitor_thread.start()

    def monitoring_loop(self):
        """Bucle de monitoreo para actualizar el estado de los bots"""
        while self.is_monitoring:
            try:
                # Para cada bot activo, envía actualizaciones a través de socketio
                bot_statuses = self.get_all_bots()

                # Convertir todas las fechas a strings en los datos de los bots
                import json
                class DateTimeEncoder(json.JSONEncoder):
                    def default(self, obj):
                        return datetime_to_string(obj)

                # Usar el encoder personalizado para enviar los datos
                socketio.emit('bot_updates', json.loads(json.dumps(bot_statuses, cls=DateTimeEncoder)))

                # Esperar antes del siguiente ciclo
                time.sleep(5)

            except Exception as e:
                logger.error(f"Error en bucle de monitoreo: {str(e)}")
                time.sleep(60)

    def save_config(self):
        """Guarda la configuración en un archivo"""
        config = {
            'exchanges': self.exchange_configs,
        }

        # Guardar configuración de bots (solo lo que se puede serializar)
        bots_config = {}
        for bot_id, bot in self.bot_instances.items():
            # Obtener atributos básicos del bot (no el objeto exchange completo)
            bot_config = {
                'exchange_name': bot.exchange.id,
                'symbol': bot.symbol,
                'timeframe': bot.timeframe,
                'market_type': bot.market_type,
                'strategy_name': bot.strategy_name,
                'investment_amount': bot.investment_amount,
                'custom_strategy': bot.custom_strategy is not None,
            }

            # Añadir parámetros de estrategia
            for param_name in [
                "ema_fast_period", "ema_slow_period", "rsi_period", "rsi_overbought", "rsi_oversold",
                "macd_fast", "macd_slow", "macd_signal", "bb_period", "bb_std", "atr_period",
                "atr_multiplier", "trailing_stop_pct", "take_profit_pct", "leverage"
            ]:
                if hasattr(bot, param_name):
                    bot_config[param_name] = getattr(bot, param_name)

            bots_config[bot_id] = bot_config

        config['bots'] = bots_config

        try:
            with open('trading_bot_config.json', 'w') as f:
                json.dump(config, f, indent=4)
        except Exception as e:
            logger.error(f"Error al guardar configuración: {str(e)}")

    def load_config(self):
        """Carga la configuración desde un archivo"""
        if not os.path.exists('trading_bot_config.json'):
            return

        try:
            with open('trading_bot_config.json', 'r') as f:
                config = json.load(f)

            # Cargar configuración de exchanges
            if 'exchanges' in config:
                self.exchange_configs = config['exchanges']

            # Cargar configuración de bots se hace al iniciar la app para poder conectar con los exchanges
            # No cargamos los bots aquí para evitar problemas con estrategias personalizadas que no estén registradas
        except Exception as e:
            logger.error(f"Error al cargar configuración: {str(e)}")

    def export_history_to_csv(self, bot_id=None):
        """Exporta el historial a CSV"""
        if bot_id:
            # Exportar historial de un bot específico
            if bot_id not in self.bot_instances:
                return False, "Bot no encontrado"

            bot = self.bot_instances[bot_id]
            if not bot.trade_history:
                return False, "No hay historial para exportar"

            filename = f"historial_{bot.exchange.id}_{bot.symbol.replace('/', '_')}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            df = pd.DataFrame(bot.trade_history)
            df.to_csv(filename, index=False)

            return True, filename
        else:
            # Exportar historial completo
            all_history = []

            for bot_id, bot in self.bot_instances.items():
                for trade in bot.trade_history:
                    # Añadir información del bot
                    trade_copy = trade.copy()
                    trade_copy['bot_id'] = bot_id
                    trade_copy['exchange'] = bot.exchange.id
                    trade_copy['symbol'] = bot.symbol
                    trade_copy['strategy'] = bot.strategy_name

                    all_history.append(trade_copy)

            if not all_history:
                return False, "No hay historial para exportar"

            filename = f"historial_completo_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            df = pd.DataFrame(all_history)
            df.to_csv(filename, index=False)

            return True, filename


# Instancia global del gestor
bot_manager = TradingBotManager()


# Rutas de la aplicación Flask
@app.route('/')
def index():
    """Página principal con lista de bots"""
    return render_template('trading_bots.html',
                           bots=bot_manager.get_all_bots(),
                           exchanges=bot_manager.exchange_configs,
                           strategies=bot_manager.strategy_profiles,
                           custom_strategies=bot_manager.get_custom_strategies())


@app.route('/bot/<bot_id>')
def view_bot(bot_id):
    """Página de detalle de un bot específico"""
    bot = bot_manager.get_bot_status(bot_id)
    if not bot:
        flash("Bot no encontrado", "error")
        return redirect(url_for('index'))

    history = bot_manager.get_bot_history(bot_id)
    return render_template('bot_detail.html', bot=bot, history=history)


@app.route('/add_bot', methods=['GET', 'POST'])
def add_bot():
    """Formulario para añadir un nuevo bot"""
    if request.method == 'POST':
        exchange_name = request.form.get('exchange')
        symbol = request.form.get('symbol')
        timeframe = request.form.get('timeframe')
        market_type = request.form.get('market_type')
        strategy_type = request.form.get('strategy_type')  # 'predefined' o 'custom'

        if strategy_type == 'predefined':
            strategy_name = request.form.get('predefined_strategy')
            custom_strategy_name = None
        else:
            strategy_name = None
            custom_strategy_name = request.form.get('custom_strategy')

        investment_amount = float(request.form.get('investment_amount'))

        success, result = bot_manager.add_bot(
            exchange_name, symbol, timeframe, market_type,
            strategy_name, investment_amount,
            custom_strategy_name=custom_strategy_name
        )

        if success:
            flash(f"Bot añadido correctamente con ID: {result}", "success")
            return redirect(url_for('index'))
        else:
            flash(f"Error al añadir bot: {result}", "error")

    return render_template('add_bot.html',
                           exchanges=bot_manager.exchange_configs,
                           strategies=bot_manager.strategy_profiles,
                           custom_strategies=bot_manager.get_custom_strategies())

@app.route('/add_exchanges', methods=['GET', 'POST'])
def add_exchanges():
    """Formulario para añadir un nuevo exchange"""
    if request.method == 'POST':
        exchange_name = request.form.get('exchange')
        api_key = request.form.get('api_key')
        api_secret = request.form.get('api_secret')
        password = request.form.get('password')

        success, message = bot_manager.add_exchange(exchange_name, api_key, api_secret, password)

        if success:
            flash(message, "success")
            return redirect(url_for('index'))
        else:
            flash(message, "error")

    return render_template('add_exchanges.html')


@app.route('/start_bot/<bot_id>', methods=['POST'])
def start_bot(bot_id):
    """Inicia un bot específico"""
    success, message = bot_manager.start_bot(bot_id)

    if success:
        flash(message, "success")
    else:
        flash(message, "error")

    return redirect(url_for('view_bot', bot_id=bot_id))


@app.route('/stop_bot/<bot_id>', methods=['POST'])
def stop_bot(bot_id):
    """Detiene un bot específico"""
    close_positions = request.form.get('close_positions') == 'true'
    success, message = bot_manager.stop_bot(bot_id, close_positions)

    if success:
        flash(message, "success")
    else:
        flash(message, "error")

    return redirect(url_for('view_bot', bot_id=bot_id))


@app.route('/close_position/<bot_id>', methods=['POST'])
def close_position(bot_id):
    """Cierra la posición de un bot específico"""
    success, message = bot_manager.close_bot_position(bot_id)

    if success:
        flash(message, "success")
    else:
        flash(message, "error")

    return redirect(url_for('view_bot', bot_id=bot_id))


@app.route('/remove_bot/<bot_id>', methods=['POST'])
def remove_bot(bot_id):
    """Elimina un bot específico"""
    success, message = bot_manager.remove_bot(bot_id)

    if success:
        flash(message, "success")
        return redirect(url_for('index'))
    else:
        flash(message, "error")
        return redirect(url_for('view_bot', bot_id=bot_id))


@app.route('/remove_exchange/<exchange_name>', methods=['POST'])
def remove_exchange(exchange_name):
    """Elimina un exchange específico"""
    success, message = bot_manager.remove_exchange(exchange_name)

    if success:
        flash(message, "success")
    else:
        flash(message, "error")

    return redirect(url_for('index'))


@app.route('/export_history/<bot_id>', methods=['GET'])
def export_history(bot_id):
    """Exporta el historial de un bot específico"""
    success, result = bot_manager.export_history_to_csv(bot_id)

    if success:
        flash(f"Historial exportado a {result}", "success")
    else:
        flash(result, "error")

    return redirect(url_for('view_bot', bot_id=bot_id))


@app.route('/export_all_history', methods=['GET'])
def export_all_history():
    """Exporta el historial de todos los bots"""
    success, result = bot_manager.export_history_to_csv()

    if success:
        flash(f"Historial exportado a {result}", "success")
    else:
        flash(result, "error")

    return redirect(url_for('index'))


@app.route('/register_custom_strategy', methods=['GET', 'POST'])
def register_custom_strategy():
    """Formulario para registrar una estrategia personalizada"""
    if request.method == 'POST':
        name = request.form.get('name')
        code = request.form.get('code')
        description = request.form.get('description', 'Estrategia personalizada')

        if not name or not code:
            flash("El nombre y el código son obligatorios", "error")
            return render_template('register_strategy.html')

        try:
            # Evaluar el código para crear la función
            local_vars = {}
            exec(code, globals(), local_vars)

            if 'custom_strategy' not in local_vars:
                flash("El código debe definir una función llamada 'custom_strategy'", "error")
                return render_template('register_strategy.html', name=name, code=code, description=description)

            strategy_func = local_vars['custom_strategy']

            # Registrar la estrategia
            success = bot_manager.register_custom_strategy(name, strategy_func, description)

            if success:
                flash(f"Estrategia '{name}' registrada correctamente", "success")
                return redirect(url_for('index'))
            else:
                flash("Error al registrar la estrategia", "error")
        except Exception as e:
            flash(f"Error en el código: {str(e)}", "error")

        return render_template('register_strategy.html', name=name, code=code, description=description)

    return render_template('register_strategy.html')


# Rutas para API
@app.route('/api/bots', methods=['GET'])
def api_get_bots():
    """API para obtener todos los bots"""
    return jsonify(bot_manager.get_all_bots())


@app.route('/api/bot/<bot_id>', methods=['GET'])
def api_get_bot(bot_id):
    """API para obtener un bot específico"""
    bot = bot_manager.get_bot_status(bot_id)
    if not bot:
        return jsonify({"error": "Bot no encontrado"}), 404
    return jsonify(bot)


@app.route('/api/exchanges', methods=['GET'])
def api_get_exchanges():
    """API para obtener todos los exchanges"""
    return jsonify({"exchanges": list(bot_manager.exchange_configs.keys())})


@app.route('/api/strategies', methods=['GET'])
def api_get_strategies():
    """API para obtener todas las estrategias"""
    strategies = {
        "predefined": bot_manager.strategy_profiles,
        "custom": bot_manager.get_custom_strategies()
    }
    return jsonify(strategies)


@app.route('/api/start_bot/<bot_id>', methods=['POST'])
def api_start_bot(bot_id):
    """API para iniciar un bot"""
    success, message = bot_manager.start_bot(bot_id)
    return jsonify({"success": success, "message": message})


@app.route('/api/stop_bot/<bot_id>', methods=['POST'])
def api_stop_bot(bot_id):
    """API para detener un bot"""
    data = request.get_json() or {}
    close_positions = data.get('close_positions', False)
    success, message = bot_manager.stop_bot(bot_id, close_positions)
    return jsonify({"success": success, "message": message})


@socketio.on('connect')
def handle_connect():
    """Maneja la conexión de un cliente websocket"""
    logger.info(f"Cliente conectado: {request.sid}")


@socketio.on('disconnect')
def handle_disconnect():
    """Maneja la desconexión de un cliente websocket"""
    logger.info(f"Cliente desconectado: {request.sid}")


@app.route('/api/exchange_pairs/<exchange_id>', methods=['GET'])
def api_get_exchange_pairs(exchange_id):
    """API para obtener pares disponibles en un exchange"""
    try:
        # Verificar que el exchange esté configurado
        if exchange_id not in bot_manager.exchange_configs:
            return jsonify({"success": False, "message": "Exchange no configurado"}), 404

        # Conectar al exchange si no está conectado
        if exchange_id not in bot_manager.exchanges:
            try:
                bot_manager.connect_exchange(exchange_id)
            except Exception as e:
                return jsonify({"success": False, "message": f"Error al conectar con el exchange: {str(e)}"}), 500

        exchange = bot_manager.exchanges[exchange_id]

        # Obtener pares disponibles
        try:
            exchange.load_markets()
            pairs = list(exchange.markets.keys())
            return jsonify({"success": True, "pairs": pairs})
        except Exception as e:
            return jsonify({"success": False, "message": f"Error al cargar mercados: {str(e)}"}), 500

    except Exception as e:
        return jsonify({"success": False, "message": f"Error: {str(e)}"}), 500

def create_app():
    """Función para crear la aplicación Flask"""
    return app


if __name__ == "__main__":
    # Iniciar la aplicación
    socketio.run(app, debug=True)