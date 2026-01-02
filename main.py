# ==============================================================================
# Проект: Бортовий Комп'ютер для Audi 80 B3 Mono Motronic на Raspberry Pi Pico
# Автор: tor4man
# Файл: Main Logic (з функціоналом палива)
# Дата оновлення: 2025-12-15
# ==============================================================================

# -------------------------------------------------------------------------
# 0. ІМПОРТ МОДУЛІВ
# -------------------------------------------------------------------------
from machine import Pin, I2C, disable_irq, enable_irq, PWM, ADC
import time
import framebuf
import os

import Settings
import Icons

try:
    import sh1107
except ImportError:
    print("Бібліотека sh1107 не знайдена. Дисплей буде вимкнено.")
    sh1107 = None

# -------------------------------------------------------------------------
# 1. ФУНКЦІЇ ДЛЯ ГРАФІКИ (SH1107 EXTENSIONS)
# -------------------------------------------------------------------------
# Тимчасовий буфер для рендерингу окремих символів (8x8 пікселів)
_temp_fb_char = None
if sh1107:
    try:
        _temp_char_buffer = bytearray(8) # Буфер 8 байт для 8x8 монохромного символу (8*8/8=8)
        _temp_fb_char = framebuf.FrameBuffer(_temp_char_buffer, 8, 8, framebuf.MONO_VLSB)
    except Exception as e:
        print(f"Помилка ініціалізації _temp_fb_char: {e}")
        _temp_fb_char = None

def _draw_stretched_char(oled_obj, char, start_x, y, size_x, size_y, c=1):
    """Малює один символ, розтягнутий до заданих розмірів."""
    if _temp_fb_char is None:
        # Fallback: якщо тимчасовий буфер не ініціалізовано, малюємо стандартний текст
        oled_obj.text(char, start_x, y, c)
        return start_x + 8 # Повертаємо стандартну ширину символу

    _temp_fb_char.fill(0) # Очищаємо буфер символу
    _temp_fb_char.text(char, 0, 0, 1) # Малюємо символ у буфері

    # Проходимо по пікселях 8x8 буфера та малюємо розтягнуті прямокутники на OLED
    for dy in range(8):
        for dx in range(8):
            if _temp_fb_char.pixel(dx, dy):
                oled_obj.fill_rect(start_x + dx * size_x, y + dy * size_y, size_x, size_y, c)
    return start_x + 8 * size_x # Повертаємо позицію для наступного символу

def stretched_text_optimized(self, s, x, y, size_x, size_y, c=1):
    """Малює рядок тексту, розтягнутий по X та Y осях."""
    current_x = x
    for char in s:
        current_x = _draw_stretched_char(self, char, current_x, y, size_x, size_y, c)

def draw_frame_rect_with_rounded_corners(self, x, y, w, h, r, c=1):
    """Малює прямокутник з заокругленими кутами (товщина 1px, r - радіус).
    Примітка: При r=2 малюються лише 8 кутових пікселів для мінімального заокруглення.
    Для більших радіусів r ця функція не малює повних дуг, а лише кутові пікселі.
    """
    # 1. Малюємо чотири прямі секції, зупиняючись за r пікселів до кута.
    self.hline(x + r, y, w - 2 * r, c)           # Верхня лінія
    self.hline(x + r, y + h - 1, w - 2 * r, c)   # Нижня лінія
    self.vline(x, y + r, h - 2 * r, c)           # Ліва лінія
    self.vline(x + w - 1, y + r, h - 2 * r, c)   # Права лінія

    # 2. Малюємо 8 кутових пікселів (для r=2)
    # TL: (x+r-1, y), (x, y+r-1)
    self.pixel(x + r - 1, y, c)
    self.pixel(x, y + r - 1, c)

    # TR: (x+w-r, y), (x+w-1, y+r-1)
    self.pixel(x + w - r, y, c)
    self.pixel(x + w - 1, y + r - 1, c)

    # BL: (x+r-1, y+h-1), (x, y+h-r)
    self.pixel(x + r - 1, y + h - 1, c)
    self.pixel(x, y + h - r, c)

    # BR: (x+w-r, y+h-1), (x+w-1, y+h-r)
    self.pixel(x + w - r, y + h - 1, c)
    self.pixel(x + w - 1, y + h - r, c)

if sh1107 and _temp_fb_char:
    sh1107.SH1107_I2C.stretched_text = stretched_text_optimized

    def large_text_wrapper(self, s, x, y, size, c=1):
        stretched_text_optimized(self, s, x, y, size, size, c)
    sh1107.SH1107_I2C.large_text = large_text_wrapper
    sh1107.SH1107_I2C.round_rect = draw_frame_rect_with_rounded_corners

# -------------------------------------------------------------------------
# 2. ВИЗНАЧЕННЯ ГРУП ПОМИЛОК
# -------------------------------------------------------------------------
# Всі активні помилки викликають звуковий сигнал і попередження.
# Цей список містить ТЕКСТИ всіх помилок, які мають викликати звук та відображатися.
ALL_SOUND_TRIGGERING_ERROR_TEXTS = [
    Icons.ERROR_ICONS['LOW_OIL']['text'],
    Icons.ERROR_ICONS['OVERHEAT']['text'], # Об'єднана іконка для перегріву та низького рівня охолоджувальної рідини
    Icons.ERROR_ICONS['BRAKE_FLUID']['text'],
    Icons.ERROR_ICONS['OIL_PRESSURE_HIGH']['text'],
]

# -------------------------------------------------------------------------
# 3. ГЛОБАЛЬНІ ЗМІННІ СТАНУ
# -------------------------------------------------------------------------
total_pulse_time_us = 0 # Загальний час відкриття форсунки за інтервал (мкс)
vss_pulse_count = 0     # Кількість імпульсів датчика швидкості за інтервал
last_pulse_edge_us = 0  # Час останнього фронту сигналу форсунки (для розрахунку total_pulse_time_us)
last_vss_pulse_us = 0   # Час останнього імпульсу VSS (для дебаунсингу)
last_inj_start_us = 0   # Час початку останнього імпульсу форсунки (для розрахунку RPM)
current_inj_period_us = 0 # Період імпульсів форсунки (для RPM)
last_inj_activity_time_ms = time.ticks_ms() # Час останньої активності форсунки
last_vss_activity_time_ms = time.ticks_ms() # Час останньої активності VSS
engine_running_start_time_ms = 0 # Час запуску двигуна (для затримки перевірки тиску масла)
trip_fuel_consumed_L = 0.0 # Накопичене паливо за поточну поїздку (TRIP)
trip_distance_travelled_km = 0.0 # Пройдена відстань за поточну поїздку (TRIP)
persistent_trip_fuel_L = 0.0 # Накопичене паливо за всю історію (PERS)
persistent_trip_distance_km = 0.0 # Пройдена відстань за всю історію (PERS)
last_persistent_save_time_ms = time.ticks_ms() # Час останнього збереження персистентних даних
last_display_update_time = time.ticks_ms() # Час останнього оновлення дисплея
blink_on = True # Статус блимання для попереджень
last_blink_toggle_time_ms = time.ticks_ms() # Час останнього перемикання статусу блимання
last_error_cycle_time_ms = time.ticks_ms() # Час останнього перемикання іконки помилки
current_speaker_freq = 0 # Поточна частота динаміка
current_speaker_duty = 0 # Поточна шпаруватість динаміка
active_errors = [] # Список активних помилок для відображення
current_error_display_index = 0 # Поточний індекс помилки в циклі відображення
sensor_alarm_active = False # Чи активний звуковий сигнал тривоги
alarm_phase = 0 # Поточна фаза звукового сигналу
alarm_phase_start_time_ms = 0 # Час початку поточної фази сигналу
_queued_errors_for_next_cycle = [] # Черга помилок для перемикання після завершення циклу
file_error_count = 0 # Лічильник помилок файлової системи

# Змінні для датчика палива та його логіки
fuel_level_adc = None # Об'єкт ADC для палива
fuel_buffer = [0] * Settings.FUEL_BUFFER_SIZE # Буфер для згладжування значень палива
last_smoothed_fuel_percent = 0.0 # Останнє згладжене значення палива
last_fuel_update_time_ms = time.ticks_ms() # Час останнього оновлення палива
is_low_fuel_active_by_hysteresis = False # Стан активації "Мало палива" з гістерезисом
low_fuel_display_state = 0 # 0: Відображаємо LOW_FUEL, 1: Відображаємо Main Screen
low_fuel_last_state_change_time_ms = time.ticks_ms() # Час останньої зміни стану відображення "Мало палива"

# -------------------------------------------------------------------------
# 4. ІНІЦІАЛІЗАЦІЯ ПІНІВ
# -------------------------------------------------------------------------
INJ_PIN = Pin(Settings.PIN_INJ, Pin.IN, Pin.PULL_UP) # Вхід сигналу форсунки (Pin 0)
VSS_PIN = Pin(Settings.PIN_VSS, Pin.IN, Pin.PULL_UP) # Вхід сигналу датчика швидкості (VSS) (Pin 1)
RESET_BUTTON_PIN = Pin(Settings.PIN_BUTTON_RESET, Pin.IN, Pin.PULL_UP) # Кнопка скидання TRIP (Pin 2)
BRAKE_FLUID_SENSOR_PIN = Pin(Settings.PIN_SENSOR_BRAKE_FLUID, Pin.IN, Pin.PULL_UP) # Датчик рівня гальмівної рідини (Pin 24)
OIL_PRESSURE_LOW_SENSOR_PIN = Pin(Settings.PIN_SENSOR_OIL_PRESSURE_LOW, Pin.IN, Pin.PULL_UP) # Датчик низького тиску мастила (Pin 7)
OVERHEAT_COOLANT_LEVEL_SENSOR_PIN = Pin(Settings.PIN_SENSOR_OVERHEAT_COOLANT_LEVEL, Pin.IN, Pin.PULL_UP) # Об'єднаний пін для датчиків перегріву двигуна та низького рівня охолоджувальної рідини (Pin 8)
OIL_PRESSURE_HIGH_SENSOR_PIN = Pin(Settings.PIN_SENSOR_OIL_PRESSURE_HIGH, Pin.IN, Pin.PULL_UP) # Датчик високого тиску мастила (Pin 10)
SPEAKER_PIN = Pin(Settings.PIN_SPEAKER, Pin.OUT) # Пін для динаміка/зумера (GPIO 12)
pwm_speaker = None # Об'єкт PWM для динаміка

# Ініціалізація ADC для датчика палива
try:
    fuel_level_adc = ADC(Pin(Settings.PIN_FUEL_LEVEL_ADC))
except Exception as e:
    fuel_level_adc = None
    print(f"Помилка ініціалізації ADC для палива: {e}")

# -------------------------------------------------------------------------
# 5. СИСТЕМНІ ФУНКЦІЇ ТА ЛОГІКА
# -------------------------------------------------------------------------

def _get_error_severity_level(error_list):
    """Визначає рівень критичності списку помилок.
    Рівні: 0 (немає помилок), 1 (некритична, без звуку), 3 (критична, зі звуком).
    """
    if not error_list or error_list == [Icons.ERROR_ICONS['NONE']]: return 0
    # Якщо є будь-яка критична помилка, повертаємо 3 (критична зі звуком)
    if any(err['text'] in ALL_SOUND_TRIGGERING_ERROR_TEXTS for err in error_list): return 3
    # Якщо є тільки "Мало палива", повертаємо 1 (некритична, без звуку)
    if any(err['text'] == Icons.ERROR_ICONS['LOW_FUEL']['text'] for err in error_list): return 1
    return 0

def manage_sensor_alarm():
    """Керує послідовністю звукової тривоги відповідно до ALARM_SEQUENCE."""
    global sensor_alarm_active, alarm_phase, alarm_phase_start_time_ms
    global pwm_speaker, current_speaker_freq, current_speaker_duty

    if pwm_speaker is None: return # Якщо динамік не ініціалізовано, нічого не робимо

    if not sensor_alarm_active:
        # Якщо тривога не активна, вимикаємо динамік і скидаємо фази
        if current_speaker_duty != 0:
            pwm_speaker.duty_u16(0)
            current_speaker_duty = 0
        alarm_phase = 0
        alarm_phase_start_time_ms = 0
        return

    current_time_ms = time.ticks_ms()
    if alarm_phase_start_time_ms == 0: alarm_phase_start_time_ms = current_time_ms # Ініціалізація часу початку фази

    phase_duration, phase_freq = Settings.ALARM_SEQUENCE[alarm_phase]

    # Перевіряємо, чи потрібно перейти до наступної фази
    if time.ticks_diff(current_time_ms, alarm_phase_start_time_ms) >= phase_duration:
        alarm_phase = (alarm_phase + 1) % len(Settings.ALARM_SEQUENCE) # Перехід до наступної фази
        alarm_phase_start_time_ms = current_time_ms # Оновлюємо час початку нової фази

        next_phase_duration, next_phase_freq = Settings.ALARM_SEQUENCE[alarm_phase]
        if next_phase_freq > 0:
            # Встановлюємо нову частоту та вмикаємо динамік
            if current_speaker_freq != next_phase_freq:
                pwm_speaker.freq(next_phase_freq)
                current_speaker_freq = next_phase_freq
            if current_speaker_duty != 32768:
                pwm_speaker.duty_u16(32768)
                current_speaker_duty = 32768
        else:
            # Вимикаємо динамік
            if current_speaker_duty != 0:
                pwm_speaker.duty_u16(0)
                current_speaker_duty = 0
    elif phase_freq > 0:
        # Якщо поточна фаза ще не завершилася і має звук, переконаємося, що динамік увімкнений з правильною частотою
        if current_speaker_freq != phase_freq:
            pwm_speaker.freq(phase_freq)
            current_speaker_freq = phase_freq
        if current_speaker_duty != 32768:
            pwm_speaker.duty_u16(32768)
            current_speaker_duty = 32768

def get_raw_fuel_percent():
    """Зчитує сирі дані ADC та перетворює їх на відсотки рівня палива (0-100%)."""
    if fuel_level_adc is None:
        return 0.0 # Якщо ADC не ініціалізовано, повертаємо 0%

    raw_adc_value = fuel_level_adc.read_u16() # Зчитуємо 16-бітне значення ADC

    # Перетворюємо сире значення ADC на відсотки
    # Пропорція: (value - min) / (max - min)
    adc_range = Settings.FUEL_ADC_MAX_RAW - Settings.FUEL_ADC_MIN_RAW
    if adc_range == 0:
        return 0.0 # Запобігаємо діленню на нуль

    percent = (raw_adc_value - Settings.FUEL_ADC_MIN_RAW) / adc_range

    percent = max(0.0, min(1.0, percent)) # Обмежуємо значення 0.0-1.0
    return percent * 100.0 # Повертаємо відсотки (0-100)

def process_fuel_smoothing():
    """Зчитує та згладжує рівень палива, застосовуючи обмеження швидкості зміни."""
    global fuel_buffer, last_smoothed_fuel_percent, last_fuel_update_time_ms

    current_time_ms = time.ticks_ms()
    time_diff_sec = time.ticks_diff(current_time_ms, last_fuel_update_time_ms) / 1000.0
    # Запобігаємо діленню на 0, але також не застосовуємо обмеження швидкості зміни, якщо час не минув.
    # Якщо time_diff_sec дуже мале, ставимо його в 1 секунду для розрахунку max_change,
    # щоб обмеження не було занадто агресивним.
    if time_diff_sec < (Settings.UPDATE_INTERVAL_SEC / 2.0): # Якщо минуло менше половини інтервалу, не обмежуємо сильно
        effective_time_diff_sec = Settings.UPDATE_INTERVAL_SEC
    else:
        effective_time_diff_sec = time_diff_sec


    new_raw_percent = get_raw_fuel_percent()

    # 1. Додаємо нове значення до буфера та видаляємо найстаріше
    fuel_buffer.pop(0)
    fuel_buffer.append(new_raw_percent)

    # 2. Обчислюємо середнє значення з буфера (згладжування)
    current_smoothed_percent = sum(fuel_buffer) / len(fuel_buffer)

    # 3. Обмежуємо швидкість зміни значення, тільки якщо пройшов достатній час
    if time_diff_sec > 0: # Застосовуємо обмеження лише якщо пройшов час з останнього оновлення
        max_change = Settings.FUEL_MAX_PERCENT_CHANGE_PER_SEC * effective_time_diff_sec
        if current_smoothed_percent > last_smoothed_fuel_percent + max_change:
            current_smoothed_percent = last_smoothed_fuel_percent + max_change
        elif current_smoothed_percent < last_smoothed_fuel_percent - max_change:
            current_smoothed_percent = last_smoothed_fuel_percent - max_change

    last_smoothed_fuel_percent = current_smoothed_percent
    last_fuel_update_time_ms = current_time_ms

    return last_smoothed_fuel_percent

def check_errors():
    """Перевіряє стан всіх датчиків і повертає список АКТИВНИХ помилок, включаючи "Мало палива"."""
    global engine_running_start_time_ms, current_inj_period_us
    global is_low_fuel_active_by_hysteresis, last_smoothed_fuel_percent

    found_errors = [] # Це список всіх знайдених помилок (критичних та некритичних)
    current_time_ms = time.ticks_ms()

    # --- 0. Визначення статусу двигуна (перевірка RPM) ---
    calculated_rpm = 0
    if current_inj_period_us > 0:
        calculated_rpm = Settings.RPM_CALCULATION_FACTOR // current_inj_period_us
    is_injector_active = (time.ticks_diff(current_time_ms, last_inj_activity_time_ms) < 1000)
    is_moving_fast = (time.ticks_diff(current_time_ms, last_vss_activity_time_ms) < 1000)
    is_engine_running_stable = (is_injector_active and calculated_rpm > Settings.MIN_RPM_FOR_STABLE_RUNNING) or is_moving_fast

    if is_engine_running_stable:
        if engine_running_start_time_ms == 0:
            engine_running_start_time_ms = current_time_ms
    else:
        engine_running_start_time_ms = 0

    # --- 1. Критичні помилки (датчики) ---
    if BRAKE_FLUID_SENSOR_PIN.value() == 0: found_errors.append(Icons.ERROR_ICONS['BRAKE_FLUID'])
    if OVERHEAT_COOLANT_LEVEL_SENSOR_PIN.value() == 0: found_errors.append(Icons.ERROR_ICONS['OVERHEAT'])
    if calculated_rpm > Settings.MIN_RPM_FOR_HIGH_PRESSURE_CHECK:
        if OIL_PRESSURE_HIGH_SENSOR_PIN.value() == 1: found_errors.append(Icons.ERROR_ICONS['OIL_PRESSURE_HIGH'])
    if OIL_PRESSURE_LOW_SENSOR_PIN.value() == 0:
        if is_engine_running_stable and engine_running_start_time_ms != 0:
            if time.ticks_diff(current_time_ms, engine_running_start_time_ms) > Settings.OIL_CHECK_DELAY_MS:
                found_errors.append(Icons.ERROR_ICONS['LOW_OIL'])

    # --- 2. Некритична помилка "Мало палива" (з гістерезисом) ---
    # Оновлюємо згладжене значення палива

    if is_low_fuel_active_by_hysteresis:
        # Якщо попередження вже активне, вимикаємо його лише коли палива стане значно більше
        if last_smoothed_fuel_percent >= Settings.FUEL_HIGH_THRESHOLD_PERCENT:
            is_low_fuel_active_by_hysteresis = False
    else:
        # Якщо попередження неактивне, активуємо його, коли палива стане менше порогового значення
        if last_smoothed_fuel_percent <= Settings.FUEL_LOW_THRESHOLD_PERCENT:
            is_low_fuel_active_by_hysteresis = True

    if is_low_fuel_active_by_hysteresis:
        found_errors.append(Icons.ERROR_ICONS['LOW_FUEL'])

    if found_errors:
        return found_errors

    return [Icons.ERROR_ICONS['NONE']] # Повертаємо "NONE", якщо помилок не знайдено

def load_persistent_data():
    """Завантажує персистентні дані TRIP та PERS з файлу."""
    global persistent_trip_fuel_L, persistent_trip_distance_km, trip_fuel_consumed_L, trip_distance_travelled_km, file_error_count
    try:
        with open(Settings.TRIP_DATA_FILE, 'r') as f:
            lines = f.readlines()
            if len(lines) >= 4:
                persistent_trip_fuel_L = float(lines[0].strip())
                persistent_trip_distance_km = float(lines[1].strip())
                trip_fuel_consumed_L = float(lines[2].strip())
                trip_distance_travelled_km = float(lines[3].strip())
    except Exception as e:
        print(f"⚠️ Load persistent data error: {e}") # Додано дебаг вивід
        file_error_count += 1

def save_persistent_data():
    """Зберігає персистентні дані TRIP та PERS до файлу з певним інтервалом."""
    global last_persistent_save_time_ms, file_error_count
    now = time.ticks_ms()
    if time.ticks_diff(now, last_persistent_save_time_ms) >= Settings.PERSISTENT_SAVE_INTERVAL_MS:
        try:
            with open(Settings.TRIP_DATA_TEMP, 'w') as f:
                f.write(str(persistent_trip_fuel_L) + '\n')
                f.write(str(persistent_trip_distance_km) + '\n')
                f.write(str(trip_fuel_consumed_L) + '\n')
                f.write(str(trip_distance_travelled_km) + '\n')

            try: os.remove(Settings.TRIP_DATA_BACKUP)
            except OSError: pass # Ігноруємо, якщо файлу немає
            try: os.rename(Settings.TRIP_DATA_FILE, Settings.TRIP_DATA_BACKUP)
            except OSError: pass # Ігноруємо, якщо файлу немає
            os.rename(Settings.TRIP_DATA_TEMP, Settings.TRIP_DATA_FILE)
            last_persistent_save_time_ms = now
        except Exception as e:
            print(f"⚠️ Save persistent data error: {e}") # Додано дебаг вивід
            file_error_count += 1

def reset_persistent_trip():
    """Автоматично скидає лічильники PERS, якщо досягнуто ліміту відстані."""
    global persistent_trip_fuel_L, persistent_trip_distance_km, file_error_count
    if persistent_trip_distance_km >= Settings.RESET_PERSISTENT_TRIP_DISTANCE_KM:
        print(f"🔄 Reset persistent trip at {persistent_trip_fuel_L:.2f}L / {persistent_trip_distance_km:.2f}km")
        persistent_trip_fuel_L = 0.0
        persistent_trip_distance_km = 0.0
        try:
            with open(Settings.TRIP_DATA_TEMP, 'w') as f: f.write("0.0\n0.0\n")
            try: os.remove(Settings.TRIP_DATA_BACKUP)
            except OSError: pass
            try: os.rename(Settings.TRIP_DATA_FILE, Settings.TRIP_DATA_BACKUP)
            except OSError: pass
            os.rename(Settings.TRIP_DATA_TEMP, Settings.TRIP_DATA_FILE)
        except OSError as e:
            print(f"⚠️ Reset persistent trip file error: {e}") # Додано дебаг вивід
            file_error_count += 1

# -------------------------------------------------------------------------
# 6. ОБРОБНИКИ ПЕРЕРИВАНЬ (IRQ)
# -------------------------------------------------------------------------

def injector_irq_handler(pin):
    """Обробник переривання форсунки: Рахує витрату палива (ширина імпульсу) та RPM (період)."""
    global total_pulse_time_us, last_pulse_edge_us, last_inj_activity_time_ms
    global last_inj_start_us, current_inj_period_us

    current_time_us = time.ticks_us()
    last_inj_activity_time_ms = time.ticks_ms()

    if pin.value() == 0: # FALLING EDGE
        if last_inj_start_us != 0:
            period = time.ticks_diff(current_time_us, last_inj_start_us)
            if period > Settings.MIN_INJ_PERIOD_US:
                current_inj_period_us = period
        last_inj_start_us = current_time_us

        if last_pulse_edge_us != 0:
            total_pulse_time_us += time.ticks_diff(current_time_us, last_pulse_edge_us)
        last_pulse_edge_us = current_time_us

    elif pin.value() == 1: # RISING EDGE
        if last_pulse_edge_us != 0:
            total_pulse_time_us += time.ticks_diff(current_time_us, last_pulse_edge_us)
        last_pulse_edge_us = 0

def vss_irq_handler(pin):
    """Обробник переривання датчика швидкості (VSS): Рахує кількість імпульсів."""
    global vss_pulse_count, last_vss_activity_time_ms, last_vss_pulse_us
    now = time.ticks_us()

    if time.ticks_diff(now, last_vss_pulse_us) > Settings.VSS_DEBOUNCE_US: # Використання константи з Settings
        vss_pulse_count += 1
        last_vss_activity_time_ms = time.ticks_ms()
        last_vss_pulse_us = now

# -------------------------------------------------------------------------
# 7. ІНІЦІАЛІЗАЦІЯ СИСТЕМИ
# -------------------------------------------------------------------------

oled_status = "OFF"
oled = None
i2c = I2C(0, scl=Pin(Settings.PIN_I2C_SCL), sda=Pin(Settings.PIN_I2C_SDA), freq=Settings.I2C_FREQ)

try:
    pwm_speaker = PWM(SPEAKER_PIN)
    pwm_speaker.freq(1000)
    pwm_speaker.duty_u16(0)
    current_speaker_duty = 0
except Exception as e:
    pwm_speaker = None
    print(f"Помилка ініціалізації динаміка: {e}")

if sh1107:
    i2c_devices = i2c.scan()
    if Settings.OLED_ADDR_HEX in i2c_devices:
        try:
            oled = sh1107.SH1107_I2C(128, 128, i2c, address=Settings.OLED_ADDR_HEX, rotate=0)
            oled_status = "OK"
            oled.contrast(Settings.OLED_CONTRAST)

            # --- ІНІЦІАЛІЗАЦІЯ ЗГЛАДЖЕННЯ ПАЛИВА ---
            # Заповнюємо буфер початковим значенням палива
            initial_fuel_percent = get_raw_fuel_percent()
            for i in range(Settings.FUEL_BUFFER_SIZE):
                fuel_buffer[i] = initial_fuel_percent
            last_smoothed_fuel_percent = initial_fuel_percent

            # --- ПЕРВИННА ПЕРЕВІРКА ПОМИЛОК ПРИ ЗАПУСКУ ---
            initial_full_errors = check_errors() # Отримуємо всі помилки (критичні та некритичні)
            # Фільтруємо критичні помилки для прийняття рішення про показ STATUS_OK
            initial_critical_errors = [err for err in initial_full_errors if err['text'] in ALL_SOUND_TRIGGERING_ERROR_TEXTS]

            if not initial_critical_errors: # Якщо КРИТИЧНИХ помилок немає при старті
                # Показуємо привітальний екран STATUS_OK
                oled.fill(0)
                if 'STATUS_OK' in Icons.ERROR_ICONS and Icons.ERROR_ICONS['STATUS_OK']['icon'] is not None:
                     fd = Icons.ERROR_ICONS['STATUS_OK']
                     ok_icon_fb = framebuf.FrameBuffer(fd['icon'], fd['width'], fd['height'], framebuf.MONO_HLSB)
                     oled.blit(ok_icon_fb, fd['icon_pos'][0], fd['icon_pos'][1])
                oled.show()
                time.sleep(Settings.STARTUP_OK_SCREEN_DURATION_SEC)
                active_errors = [Icons.ERROR_ICONS['NONE']] # Починаємо з чистого стану, далі логіка в циклі обробить
            else:
                # Якщо є КРИТИЧНІ помилки, одразу показуємо їх
                active_errors = initial_critical_errors[:] # Копіюємо знайдені критичні помилки
                if 'WARNING' in Icons.ERROR_ICONS:
                    active_errors.append(Icons.ERROR_ICONS['WARNING'])

                # Активуємо звуковий сигнал для критичних помилок
                if pwm_speaker:
                    sensor_alarm_active = True
                    alarm_phase = 0
                    alarm_phase_start_time_ms = 0
                    if pwm_speaker.freq() != Settings.ALARM_SEQUENCE[0][1]:
                         pwm_speaker.freq(Settings.ALARM_SEQUENCE[0][1])
                    pwm_speaker.duty_u16(32768)
                    current_speaker_freq = Settings.ALARM_SEQUENCE[0][1]
                    current_speaker_duty = 32768

                # Відображаємо першу критичну помилку зі списку на старті
                oled.fill(0)
                icon_to_draw = active_errors[0]
                if icon_to_draw['icon'] is not None:
                    icon_fb = framebuf.FrameBuffer(icon_to_draw['icon'], icon_to_draw['width'], icon_to_draw['height'], framebuf.MONO_HLSB)
                    oled.blit(icon_fb, icon_to_draw['icon_pos'][0], icon_to_draw['icon_pos'][1])
                oled.show()
                time.sleep(Settings.STARTUP_ERROR_SCREEN_DURATION_SEC)
        except Exception as e:
            print(f"Помилка ініціалізації OLED: {e}")
            oled_status = "OFF"; oled = None
    else:
        print("OLED дисплей не знайдено.")
else:
    print("Драйвер sh1107 відсутній.")

INJ_PIN.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING, handler=injector_irq_handler)
VSS_PIN.irq(trigger=Pin.IRQ_RISING, handler=vss_irq_handler)

load_persistent_data()
try: os.remove(Settings.TRIP_DATA_TEMP)
except OSError: pass

if oled_status == "OK" and oled: oled.fill(0); oled.show()

# -------------------------------------------------------------------------
# 8. ЛОГІКА ВІДОБРАЖЕННЯ ЕКРАНІВ
# -------------------------------------------------------------------------

def draw_main_screen(oled_obj, current_distance_km_interval, current_volume_L_interval, current_speed_kmh, interval_sec):
    """Малює головний екран: миттєва витрата, статистика TRIP/PERS.
    Включає обчислення рамки навколо основного показника та одиниць.
    """
    global trip_fuel_consumed_L, trip_distance_travelled_km
    global persistent_trip_fuel_L, persistent_trip_distance_km
    global blink_on, file_error_count

    # --- 1. Розрахунок та відображення поточної витрати (L/H або L/100KM) ---
    raw_volume_l_per_h = current_volume_L_interval / (interval_sec / 3600.0) if interval_sec > 0 else 0.0
    can_show_l100km = (current_speed_kmh >= Settings.MIN_SPEED_FOR_L100KM_KMH) and \
                      (trip_distance_travelled_km >= Settings.MIN_DISTANCE_FOR_L100KM_KM)

    raw_value = 0.0
    if can_show_l100km:
        raw_value = (current_volume_L_interval / current_distance_km_interval) * 100.0 if current_distance_km_interval > 0.0001 else 0.0
        if raw_value > Settings.MAX_DISPLAY_L100KM_VALUE:
            raw_value = 0.0
    else:
        raw_value = raw_volume_l_per_h

    value_str = ""
    text_size_main = Settings.MAIN_VALUE_FONT_SIZE
    if raw_value < Settings.STATIONARY_THRESHOLD:
        value_str = "-.--" # Порожній показник (4 символи)
    elif raw_value >= 100.0 and not can_show_l100km:
        value_str = "{: >4}".format("EEEE") # "NO" лише для L/H, форматуємо до 4 символів
    else:
        # УНІВЕРСАЛЬНЕ ФОРМАТУВАННЯ ДЛЯ ГОЛОВНОГО ПОКАЗНИКА (L/H АБО L/100KM)
        # Забезпечує фіксовану ширину 4 символи та бажану кількість знаків після коми.
        if raw_value < 10.0:
            value_str = "{: >4.2f}".format(raw_value) # Наприклад: " 1.20"
        else:
            value_str = "{: >4.1f}".format(raw_value) # Наприклад: "12.3"

    # Позиції та розміри основного значення
    main_val_x_pos = (128 - len(value_str) * 8 * text_size_main) // 2 + Settings.MAIN_VALUE_X_OFFSET
    main_val_y_pos = Settings.MAIN_VALUE_Y_POS
    main_val_width = len(value_str) * 8 * text_size_main
    main_val_height = 8 * text_size_main

    oled_obj.large_text(value_str, main_val_x_pos, main_val_y_pos, text_size_main, 1)

    # --- 2. Відображення одиниць виміру (L/H або L/100KM) ---
    unit_text = "L/H"
    if can_show_l100km:
        unit_text = "L/100KM" # Повна одиниця виміру

    # Позиції та розміри одиниць виміру
    unit_text_size_x = Settings.MAIN_UNIT_FONT_SIZE_X
    unit_text_size_y = Settings.MAIN_UNIT_FONT_SIZE_Y
    unit_y_pos = main_val_y_pos + main_val_height + Settings.MAIN_UNIT_Y_OFFSET
    unit_text_width = len(unit_text) * 8 * unit_text_size_x
    unit_text_height = 8 * unit_text_size_y
    # Центруємо одиниці виміру відносно основного значення
    unit_x_pos = (128 - unit_text_width) // 2 + Settings.MAIN_VALUE_X_OFFSET

    oled_obj.stretched_text(unit_text, unit_x_pos, unit_y_pos, unit_text_size_x, unit_text_size_y, 1)

    # --- 3. Малювання рамки навколо основного показника та одиниць ---
    padding = Settings.MAIN_FRAME_PADDING_PX
    radius = Settings.MAIN_FRAME_RADIUS_PX

    # Обчислення об'єднаних границь
    frame_x_min = min(main_val_x_pos, unit_x_pos)
    frame_x_max = max(main_val_x_pos + main_val_width, unit_x_pos + unit_text_width)
    frame_y_min = main_val_y_pos
    frame_y_max = unit_y_pos + unit_text_height

    # Координати та розміри рамки з урахуванням відступу
    frame_x = frame_x_min - padding
    frame_y = frame_y_min - padding
    frame_w = (frame_x_max - frame_x_min) + 2 * padding
    frame_h = (frame_y_max - frame_y_min) + 2 * padding

    oled_obj.round_rect(frame_x, frame_y, frame_w, frame_h, radius, 1)

    # --- 4. Статистика PERS (середня витрата L/100KM) ---
    avg_p_val = 0.0
    if persistent_trip_distance_km > Settings.MIN_PERS_DISPLAY_DISTANCE_KM:
        avg_p_val = (persistent_trip_fuel_L / persistent_trip_distance_km) * 100.0

    if avg_p_val > 0.0 and avg_p_val <= Settings.MAX_DISPLAY_L100KM_VALUE:
        pers_avg_str = "{:>{}.1f}".format(avg_p_val, Settings.PERS_L100KM_DISPLAY_WIDTH)
    else:
        pers_avg_str = "{:>{}}".format("----", Settings.PERS_L100KM_DISPLAY_WIDTH)

    pers_txt = "{} L/100KM".format(pers_avg_str)
    oled_obj.stretched_text(pers_txt, Settings.STAT_TEXT_X_POS, Settings.PERS_STAT_Y_POS_FUEL_LESS, 1, 2)

    # --- 5. Статистика TRIP (накопичені літри та кілометри) ---
    f_str_val = trip_fuel_consumed_L if trip_fuel_consumed_L > 0.05 else None
    f_str_display = "{:>{}.1f}".format(f_str_val, Settings.TRIP_FUEL_DISPLAY_WIDTH) if f_str_val is not None else "{:>{}}".format("----", Settings.TRIP_FUEL_DISPLAY_WIDTH)

    d_str_val = int(trip_distance_travelled_km) if trip_distance_travelled_km > 0.1 else None
    d_str_display = "{:>{}.0f}".format(d_str_val, Settings.TRIP_DISTANCE_DISPLAY_WIDTH) if d_str_val is not None else "{:>{}}".format("---", Settings.TRIP_DISTANCE_DISPLAY_WIDTH)

    trip_txt = "{}L  {}KM".format(f_str_display, d_str_display)
    oled_obj.stretched_text(trip_txt, Settings.STAT_TEXT_X_POS, Settings.TRIP_STAT_Y_POS_FUEL_LESS, 1, 2)

    # --- 6. Відображення лічильника файлових помилок (якщо є) ---
    if file_error_count > 0:
        error_display_text = f"FE:{file_error_count}"
        text_w = len(error_display_text) * 8 # Стандартний шрифт 8px
        oled_obj.text(error_display_text, 108 - text_w, 0, 1) # У верхньому правому куті

def calculate_and_display(interval_sec=1):
    """Основний цикл логіки: збір даних, розрахунки, обробка помилок та оновлення дисплея."""
    global trip_fuel_consumed_L, trip_distance_travelled_km, persistent_trip_fuel_L, persistent_trip_distance_km
    global low_fuel_display_state, low_fuel_last_state_change_time_ms

    # Захист від переповнення лічильників TRIP згідно з MAX_TRIP_LITERS та MAX_TRIP_DISTANCE
    if trip_fuel_consumed_L > Settings.MAX_TRIP_LITERS or trip_distance_travelled_km > Settings.MAX_TRIP_DISTANCE:
        trip_fuel_consumed_L = 0.0
        trip_distance_travelled_km = 0.0

    global total_pulse_time_us, vss_pulse_count
    global blink_on, last_blink_toggle_time_ms
    global active_errors, current_error_display_index, last_error_cycle_time_ms
    global sensor_alarm_active, alarm_phase, alarm_phase_start_time_ms
    global current_speaker_freq, current_speaker_duty
    global _queued_errors_for_next_cycle
    global current_inj_period_us
    global last_persistent_save_time_ms
    global file_error_count

    current_time_ms = time.ticks_ms()

    # 1. Атомарне зчитування IRQ лічильників для уникнення race conditions
    state = disable_irq()
    pulses_to_process = vss_pulse_count
    pulse_time_to_process_us = total_pulse_time_us
    current_inj_period_us_atomic = current_inj_period_us
    vss_pulse_count = 0
    total_pulse_time_us = 0
    enable_irq(state)

    current_inj_period_us = current_inj_period_us_atomic

    # 3. Розрахунки на основі зібраних даних
    distance_km_current_interval = pulses_to_process / Settings.VSS_IMPULSES_PER_KM
    FUEL_RATE_L_PER_US = Settings.INJ_FLOW_RATE_ML_PER_MIN / (1000 * 60 * 1_000_000)
    volume_L_current_interval = pulse_time_to_process_us * FUEL_RATE_L_PER_US
    current_speed_kmh = (distance_km_current_interval / (interval_sec / 3600.0))

    # 4. Накопичення і збереження даних поїздок
    trip_fuel_consumed_L += volume_L_current_interval
    trip_distance_travelled_km += distance_km_current_interval

    if current_speed_kmh >= Settings.MIN_SPEED_FOR_PERS_COUNT_KMH:
        persistent_trip_fuel_L += volume_L_current_interval
        persistent_trip_distance_km += distance_km_current_interval

    reset_persistent_trip();  save_persistent_data()

    # 5. Обробка помилок
    # Оновлення згладженого значення палива перед перевіркою помилок
    process_fuel_smoothing() # Викликаємо тут, щоб last_smoothed_fuel_percent був актуальним
    real_sensor_errors = check_errors()

    # Визначаємо, чи є критичні помилки
    has_critical_errors = any(err['text'] in ALL_SOUND_TRIGGERING_ERROR_TEXTS for err in real_sensor_errors)
    # Визначаємо, чи є помилка "Мало палива"
    has_low_fuel_error = any(err['text'] == Icons.ERROR_ICONS['LOW_FUEL']['text'] for err in real_sensor_errors)

    errors_to_show_based_on_sensors = []
    if has_critical_errors:
        # Якщо є критичні помилки, відображаємо їх (ігноруючи "Мало палива")
        errors_to_show_based_on_sensors = [err for err in real_sensor_errors if err['text'] in ALL_SOUND_TRIGGERING_ERROR_TEXTS]
        if 'WARNING' in Icons.ERROR_ICONS:
            errors_to_show_based_on_sensors.append(Icons.ERROR_ICONS['WARNING'])
    elif has_low_fuel_error:
        # Якщо є тільки "Мало палива", активуємо його спеціальний цикл відображення
        errors_to_show_based_on_sensors = [Icons.ERROR_ICONS['LOW_FUEL']]
    else:
        # Немає активних помилок
        errors_to_show_based_on_sensors = [Icons.ERROR_ICONS['NONE']]

    # 5.2. ЛОГІКА ФІКСАЦІЇ ТА ЧЕРГИ ПОМИЛОК (LATCH QUEUE)
    current_severity = _get_error_severity_level(active_errors)
    new_severity = _get_error_severity_level(errors_to_show_based_on_sensors)

    # Визначення інтервалу циклу залежить від критичності помилок
    current_cycle_interval = Settings.ERROR_DISPLAY_CYCLE_MS # Для всіх помилок

    # Умови для НЕГАЙНОГО перемикання екрану помилок
    should_switch_immediately = new_severity > current_severity or \
                                (current_severity == 0 and new_severity > 0) or \
                                (active_errors == [Icons.ERROR_ICONS['NONE']] and errors_to_show_based_on_sensors != [Icons.ERROR_ICONS['NONE']]) or \
                                (new_severity == 0 and current_severity > 0) # Перехід з будь-якої помилки на NONE

    if should_switch_immediately:
        if active_errors != errors_to_show_based_on_sensors:
            active_errors = errors_to_show_based_on_sensors[:]
            _queued_errors_for_next_cycle = []
            current_error_display_index = 0
            last_error_cycle_time_ms = current_time_ms
            # Скидаємо стан відображення "Мало палива" при негайному перемиканні
            low_fuel_display_state = 0
            low_fuel_last_state_change_time_ms = current_time_ms
    elif active_errors != errors_to_show_based_on_sensors:
        if errors_to_show_based_on_sensors != _queued_errors_for_next_cycle:
            _queued_errors_for_next_cycle = errors_to_show_based_on_sensors[:]

    time_since_last_switch = time.ticks_diff(current_time_ms, last_error_cycle_time_ms)
    is_cycle_complete = (time_since_last_switch >= current_cycle_interval) and \
                        (current_error_display_index == len(active_errors) - 1)

    if is_cycle_complete and _queued_errors_for_next_cycle:
        active_errors = _queued_errors_for_next_cycle[:]
        _queued_errors_for_next_cycle = []
        current_error_display_index = 0
        last_error_cycle_time_ms = current_time_ms
        # Скидаємо стан відображення "Мало палива" при перемиканні з черги
        low_fuel_display_state = 0
        low_fuel_last_state_change_time_ms = current_time_ms

    # 5.3. Керування ЗВУКОМ та БЛИМАННЯМ
    # Звук активується тільки для критичних помилок (рівень 3)
    loud_alarm_needed = (_get_error_severity_level(active_errors) == 3)

    if pwm_speaker:
        if loud_alarm_needed and not sensor_alarm_active:
            sensor_alarm_active = True
            alarm_phase = 0; alarm_phase_start_time_ms = 0
            if pwm_speaker.freq() != Settings.ALARM_SEQUENCE[0][1]:
                 pwm_speaker.freq(Settings.ALARM_SEQUENCE[0][1])
            pwm_speaker.duty_u16(32768)
            current_speaker_freq = Settings.ALARM_SEQUENCE[0][1]
            current_speaker_duty = 32768
        elif not loud_alarm_needed and sensor_alarm_active:
            sensor_alarm_active = False

    manage_sensor_alarm()

    if time.ticks_diff(current_time_ms, last_blink_toggle_time_ms) >= Settings.BLINK_INTERVAL_MS:
        blink_on = not blink_on
        last_blink_toggle_time_ms = current_time_ms

    # 5.4. Відображення на OLED
    if oled_status != "OK" or oled is None: return

    # --- СПЕЦІАЛЬНА ЛОГІКА ДЛЯ ВІДОБРАЖЕННЯ "МАЛО ПАЛИВА" ---
    current_error_severity = _get_error_severity_level(active_errors)
    if current_error_severity == 1 and active_errors[0]['text'] == Icons.ERROR_ICONS['LOW_FUEL']['text']:
        # Ми в стані відображення "Мало палива"
        time_since_low_fuel_state_change = time.ticks_diff(current_time_ms, low_fuel_last_state_change_time_ms)

        if low_fuel_display_state == 0: # Стан: Показуємо "Мало палива"
            if time_since_low_fuel_state_change >= Settings.LOW_FUEL_DISPLAY_DURATION_MS:
                low_fuel_display_state = 1 # Переходимо до показу головного екрану
                low_fuel_last_state_change_time_ms = current_time_ms

            oled.fill(0)
            icon_to_draw = Icons.ERROR_ICONS['LOW_FUEL'] # Завжди показуємо іконку "Мало палива"
            if icon_to_draw['icon'] is not None:
                icon_fb = framebuf.FrameBuffer(icon_to_draw['icon'], icon_to_draw['width'], icon_to_draw['height'], framebuf.MONO_HLSB)
                oled.blit(icon_fb, icon_to_draw['icon_pos'][0], icon_to_draw['icon_pos'][1])
            if file_error_count > 0: # Додаємо відображення помилок файлу, якщо є
                error_display_text = f"FE:{file_error_count}"
                text_w = len(error_display_text) * 8
                oled.text(error_display_text, 108 - text_w, 0, 1)
            oled.show()

        elif low_fuel_display_state == 1:  # Стан: Показуємо головний екран
            if time_since_low_fuel_state_change >= Settings.LOW_FUEL_MAIN_SCREEN_DURATION_MS:
                low_fuel_display_state = 0  # Переходимо до показу "Мало палива"
                low_fuel_last_state_change_time_ms = current_time_ms

            oled.fill(0)
            draw_main_screen(
                oled,
                distance_km_current_interval,
                volume_L_current_interval,
                current_speed_kmh,
                interval_sec
            )

            # Показуємо лічильник файлових помилок також на головному екрані
            if file_error_count > 0:
                error_display_text = f"FE:{file_error_count}"
                text_w = len(error_display_text) * 8
                oled.text(error_display_text, 108 - text_w, 0, 1)

            oled.show()
        return  # Важливо: виходимо, бо спеціальна low fuel-логіка вже все намалювала

    # --- СТАНДАРТНА ЛОГІКА ВІДОБРАЖЕННЯ (для головного екрану або критичних помилок) ---
    if active_errors == [Icons.ERROR_ICONS['NONE']]:
        oled.fill(0)
        draw_main_screen(oled, distance_km_current_interval, volume_L_current_interval, current_speed_kmh, interval_sec)
        oled.show()
        return

    display_cycle_interval = Settings.ERROR_DISPLAY_CYCLE_MS

    if current_error_display_index >= len(active_errors):
        current_error_display_index = 0

    error_to_display = active_errors[current_error_display_index]
    oled.fill(0)

    if time.ticks_diff(current_time_ms, last_error_cycle_time_ms) >= display_cycle_interval:
        current_error_display_index = (current_error_display_index + 1) % len(active_errors)
        last_error_cycle_time_ms = current_time_ms

    if error_to_display['icon'] is not None:
        icon_fb = framebuf.FrameBuffer(error_to_display['icon'], error_to_display['width'], error_to_display['height'], framebuf.MONO_HLSB)
        oled.blit(icon_fb, error_to_display['icon_pos'][0], error_to_display['icon_pos'][1])

    if file_error_count > 0: # Додаємо відображення помилок файлу, якщо є
        error_display_text = f"FE:{file_error_count}"
        text_w = len(error_display_text) * 8
        oled.text(error_display_text, 108 - text_w, 0, 1)

    oled.show()

# -------------------------------------------------------------------------
# 9. ГОЛОВНИЙ ЦИКЛ (MAIN LOOP)
# -------------------------------------------------------------------------

print("✅ БК запущено: Audi 80 Mono Motronic-1.2.3 2.0E")

while True:
    if RESET_BUTTON_PIN.value() == 0:
        trip_fuel_consumed_L = 0.0
        trip_distance_travelled_km = 0.0
        if 'pwm_speaker' in globals() and pwm_speaker:
            pwm_speaker.freq(2000)
            pwm_speaker.duty_u16(32768)
            time.sleep(0.1)
            pwm_speaker.duty_u16(0)
        while RESET_BUTTON_PIN.value() == 0:
            time.sleep(0.01)

    try:
        current_time = time.ticks_ms()
        actual_interval_sec = time.ticks_diff(current_time, last_display_update_time) / 1000.0
        if actual_interval_sec == 0: actual_interval_sec = Settings.UPDATE_INTERVAL_SEC

        calculate_and_display(actual_interval_sec)

        last_display_update_time = current_time
        time.sleep(Settings.UPDATE_INTERVAL_SEC)
    except Exception as e:
        print(f"Loop Error: {e}")
        if oled_status == "OK" and oled:
            oled.fill(0)
            oled.large_text(
                "LOOP ERR",
                Settings.LOOP_ERROR_X_POS,
                Settings.LOOP_ERROR_Y_POS,
                Settings.LOOP_ERROR_TEXT_SIZE,
                1
            )
            oled.show()
        if pwm_speaker:
            pwm_speaker.duty_u16(0)
            current_speaker_duty = 0 # Додано скидання стану динаміка
        time.sleep(5)

