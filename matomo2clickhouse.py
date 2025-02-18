# -*- coding: utf-8 -*-
# matomo2clickhouse
# https://github.com/dneupokoev/matomo2clickhouse
#
# Replication Matomo from MySQL to ClickHouse
# Репликация Matomo: переливка данных из MySQL в ClickHouse
#
dv_file_version = '240524.01'
#
# 240524.01
# + убрал в settings.py "SETTINGS mutations_sync = 1" из sql, которые выполняются в самом конце, т.к. они стали отваливаться по таймауту, а смысла ждать ответа нет (выполнится нормально в фоне)
#
# 231201.01
# + добавил возможность подключения к базе данных MySQL через SSH (дополнительные параметры подключения: settings.SSH_*)
# + добавил и изменил информативность ошибок (чтобы легче искать причины сбоев)
#
# 231123.01
# + добавил возможность подключения к базе данных MySQL через SSH
# + необходимо установить пакет: pipenv install sshtunnel
# + необходимо заполнить дополнительные параметры подключения: settings.SSH_*
#
# 231122.01
# + после ошибки теперь будет обрабатываться не всё заданное количество, а примерно в тысячу раз меньше = (settings.replication_batch_size // 1000) + 10
#
# 231117.01
# + добавил параметр settings.CONST_TBL_NOT_DELETE_OLD - словарь с таблицами, для которых не надо удалять старые данные, если они удалены в самом matomo.
# + добавил проверку даты для строк delete (если дата старая, то игнорируем эту строку и выполнять на итоговой БД не будем: обработчик для settings.CONST_TBL_NOT_DELETE_OLD)
# + считаю количество отклоненных удалений и вывожу в лог
# + немного почистил код от рудиментов
#
# 230727.01
# + добавил settings.sql_execute_at_end_matomo2clickhouse: скрипты, которые выполнятся в конце работы matomo2clickhouse (можно использовать для удаления дублей или для других задач)
#
# 230719.01:
# + отключил асинхронность мутаций (update и delete теперь будут ждать завершения мутаций на данном сервере), чтобы не отваливалось из-за большого числа delete
# + исправил глюк с инсертом и апдейтом одной записи, когда они идут подряд (с очень маленьким интервалом) и пишутся в таблицы из settings.tables_not_updated
#
# 230505.02:
# + исправил ошибку обработки одинарной кавычки в запросе: добавил перед кавычкой экранирование, чтобы sql-запрос отрабатывал корректно
# + добавил автоматическое изменение на построчное выполнение запросов (при следующем запуске) после ошибки выполнения запроса - необходимо для определения проблемного запроса без изменения параметров: запрос будет в логе в строке с dv_sql_for_execute_last после повторного запуска после появления ошибки
# + добавил больше логирования
#
# 230406.01:
# + для ускорения изменил алгоритм: теперь запросы группируются, собираются в батчи и выполняются сразу партиями (обработка ускорилась примерно в 12 раз). Для тонкой настройки можно "поиграть" параметром settings.replication_batch_sql
#
# 230403.01:
# + добавил параметр settings.EXECUTE_CLICKHOUSE (нужен для тестирования) - True: выполнять insert в ClickHouse (боевой режим); False: не выполнять insert (для тестирования и отладки)
# + изменил параметр settings.CH_matomo_dbname - теперь базы в MySQL и ClickHouse могут иметь разные названия
# + изменил проверку исключения для построчного выполнения (dv_find_text)
#
# 221206.03:
# + базовая стабильная версия (полностью протестированная и отлаженная)
#

import settings
import os
import re
import sys
import platform
import datetime
import time
import pymysql
import paramiko
import sshtunnel
import configparser
import json
from pymysqlreplication import BinLogStreamReader
from pymysqlreplication.event import QueryEvent, RotateEvent, FormatDescriptionEvent
from binlog2sql_util import command_line_args, concat_sql_from_binlog_event, create_unique_file, temp_open, \
    reversed_lines, is_dml_event, event_type, get_dateid
from binlog2sql_util import binlog2sql_util_version
from clickhouse_driver import Client
#
# Настраиваем предупреждения
import warnings

# "default" - распечатать первое появление соответствующих предупреждений для каждого местоположения (модуль + номер строки), где выдается предупреждение
# "error" - превратить соответствующие предупреждения в исключения
# "ignore" - никогда не печатать соответствующие предупреждения
# "always" - всегда печатать соответствующие предупреждения
# "module" - распечатать первое появление соответствующих предупреждений для каждого модуля, в котором выдается предупреждение (независимо от номера строки)
# "once" - напечатать только первое появление соответствующих предупреждений, независимо от местоположения
warnings.filterwarnings("ignore")
#
#
#
from pathlib import Path

try:  # from project
    dv_path_main = f"{Path(__file__).parent}/"
    dv_file_name = f"{Path(__file__).name}"
except:  # from jupiter
    dv_path_main = f"{Path.cwd()}/"
    dv_path_main = dv_path_main.replace('jupyter/', '')
    dv_file_name = 'unknown_file'

# # Snoop - это пакет Python, который печатает строки выполняемого кода вместе со значениями каждой переменной (декоратор #@snoop)
# import snoop

# # Heartrate - визуализирует выполнение программы на Python в режиме реального времени: http://localhost:9999
# import heartrate
# heartrate.trace(browser=True)

# импортируем библиотеку для логирования
from loguru import logger


def log_message_secret(message: str):
    '''
    Функция скрывает конфиденциальную информацию в строке, например такую конструкцию {'my_token': '1111111'} на такую {'my_token': 'secret'}
    '''
    message = re.sub(r"'([^']*token[^']*)':[ ]{0,1}'[^']*'", r"'\1': 'secret'", message, count=0)
    message = re.sub(r"'([^']*passw[^']*)':[ ]{0,1}'[^']*'", r"'\1': 'secret'", message, count=0)
    return message


def log_format_secret(record):
    '''
    Задаем формат лога с заменой секретных данных (чтобы не слить в логах пароли и токены)
    '''
    record["extra"]["message_secret"] = log_message_secret(record["message"])
    return "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{extra[message_secret]}</level>\n{exception}"


def log_loguru_settings():
    '''
    Настройка логирования (куда логировать, что логировать, как логировать)
    Вызывать функцию нужно в самом начале скрипта, чтобы сразу писать лог в правильное место
    '''
    # logger.add("log/" + dv_file_name + ".json", level="DEBUG", rotation="00:00", retention='30 days', compression="gz", encoding="utf-8", serialize=True)
    # logger.add("log/" + dv_file_name + ".json", level="WARNING", rotation="00:00", retention='30 days', compression="gz", encoding="utf-8", serialize=True)
    # logger.add("log/" + dv_file_name + ".json", level="INFO", rotation="00:00", retention='30 days', compression="gz", encoding="utf-8", serialize=True)
    # logger.add(settings.PATH_TO_LOG + dv_file_name + ".log", level="INFO", rotation="0.5 GB", retention='30 days', compression="gz", encoding="utf-8")
    logger.remove()  # отключаем стандартное логирование в консоль
    if settings.DEBUG is True:
        dv_logger_level = "DEBUG"
    else:
        dv_logger_level = "INFO"
    logger.add(sys.stderr, level=dv_logger_level, format=log_format_secret, colorize=True)
    dv_logger_file = settings.PATH_TO_LOG + '/' + dv_file_name + ".log"
    logger.add(dv_logger_file, level=dv_logger_level, format=log_format_secret,
               rotation="00:00", retention='30 days', compression="gz", encoding="utf-8", enqueue=True,
               backtrace=True, diagnose=True, catch=True)


# Настраиваем логирование
log_loguru_settings()
#
logger.info(f'***')
logger.info(f'BEGIN')
try:
    # Получаем версию ОС
    logger.info(f'os.version = {platform.platform()}')
except Exception as ERROR:
    # Не удалось получить версию ОС
    logger.error(f'ERROR - os.version: {ERROR = }')
try:
    # Получаем версию питона
    logger.info(f'python.version = {sys.version}')
except Exception as ERROR:
    # Не удалось получить версию питона
    logger.error(f'ERROR - python.version: {ERROR = }')
logger.info(f'{dv_file_version = }')
logger.info(f'{binlog2sql_util_version = }')
logger.info(f'{dv_path_main = }')
logger.info(f'{dv_file_name = }')
logger.info(f'{settings.PATH_TO_LIB = }')
logger.info(f'{settings.PATH_TO_LOG = }')
logger.info(f'{settings.DEBUG = }')
try:
    logger.info(f'{settings.SSH_MySQL_CONNECT = }')
    dv_SSH_MySQL_CONNECT = settings.SSH_MySQL_CONNECT
except:
    logger.info(f'settings.SSH_MySQL_CONNECT = None')
    dv_SSH_MySQL_CONNECT = False
try:
    logger.info(f'{settings.EXECUTE_CLICKHOUSE = }')
    dv_EXECUTE_CLICKHOUSE = settings.EXECUTE_CLICKHOUSE
except:
    logger.info(f'settings.EXECUTE_CLICKHOUSE = None')
    dv_EXECUTE_CLICKHOUSE = True
logger.info(f'{dv_EXECUTE_CLICKHOUSE = }')
logger.info(f'{settings.replication_batch_size = }')
logger.info(f'{settings.replication_batch_sql = }')
logger.info(f'{settings.replication_max_number_files_per_session = }')
logger.info(f'{settings.replication_max_minutes = }')
try:
    settings_replication_max_minutes = int(settings.replication_max_minutes)
    if settings_replication_max_minutes > 10:
        settings_replication_max_minutes = settings_replication_max_minutes - 3
except:
    settings_replication_max_minutes = 5
logger.info(f'{settings_replication_max_minutes = }')
#
logger.info(f'{settings.LEAVE_BINARY_LOGS_IN_DAYS = }')
logger.info(f'{settings.replication_tables = }')
logger.info(f'{settings.tables_not_updated = }')
logger.info(f'{settings.CONST_TBL_NOT_DELETE_OLD = }')
logger.info(f'{settings.sql_execute_at_end_matomo2clickhouse = }')
#
#
# dv_find_text - переменная проверки исключения для построчного выполнения (dv_find_text)
# \a 	Гудок встроенного в систему динамика.
# \b 	Backspace, он же возврат, он же "пробел назад" – удаляет один символ перед курсором.
# \f 	Разрыв страницы.
# \n 	Перенос строки (новая строка).
# \r 	Возврат курсора в начало строки.
# \t 	Горизонтальный отступ слева от начала строки (горизонтальная табуляция).
# \v 	Вертикальный отступ сверху (вертикальная табуляция).
# \0 	Символ Null.
#
dv_find_text = re.compile(r'(\f|\n|\r|\t|\v|\0)')


#
#
#
#@snoop
def create_ssh_tunnel():
    '''
    Функция создает SSH-туннель и возвращает данные о туннеле
    в tunnel.local_bind_port будет порт созданного туннеля
    '''
    # Создание SSH-туннеля
    tunnel = sshtunnel.SSHTunnelForwarder(
        (settings.SSH_MySQL_HOST, settings.SSH_MySQL_PORT),
        ssh_username=settings.SSH_MySQL_USERNAME,
        ssh_password=settings.SSH_MySQL_PASSWORD,
        remote_bind_address=(settings.MySQL_matomo_host, settings.MySQL_matomo_port)
    )
    tunnel.start()
    return tunnel


def get_now():
    '''
    Функция возвращает текущую дату и время в заданном формате
    '''
    logger.debug(f"get_now")
    dv_time_begin = time.time()
    dv_created = f"{datetime.datetime.fromtimestamp(dv_time_begin).strftime('%Y-%m-%d %H:%M:%S')}"
    # dv_created = f"{datetime.datetime.fromtimestamp(dv_time_begin).strftime('%Y-%m-%d %H:%M:%S.%f')}"
    return dv_created


def get_second_between_now_and_datetime(in_datetime_str='2000-01-01 00:00:00'):
    '''
    Функция возвращает количество секунд между текущим временем и полученной датой-временем в формате '%Y-%m-%d %H:%M:%S'
    '''
    logger.debug(f"get_second_between_now_and_datetime")
    tmp_datetime_start = datetime.datetime.strptime(in_datetime_str, '%Y-%m-%d %H:%M:%S')
    tmp_now = datetime.datetime.strptime(datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S'),
                                         '%Y-%m-%d %H:%M:%S')
    tmp_seconds = int((tmp_now - tmp_datetime_start).total_seconds())
    return tmp_seconds


#@snoop
def get_disk_space():
    '''
    Функция возвращает информацию о свободном месте на диске в гигабайтах
    dv_statvfs_bavail = Количество свободных гагабайтов, которые разрешено использовать обычным пользователям (исключая зарезервированное пространство)
    dv_statvfs_blocks = Размер файловой системы в гигабайтах
    dv_result_bool = true - корректно отработало, false - получить данные не удалось
    '''
    logger.debug(f"get_disk_space")
    dv_statvfs_blocks = 999999
    dv_statvfs_bavail = dv_statvfs_blocks
    dv_result_bool = False
    try:
        # получаем свободное место на диске ubuntu
        statvfs = os.statvfs('/')
        # Size of filesystem in bytes (Размер файловой системы в байтах)
        dv_statvfs_blocks = round((statvfs.f_frsize * statvfs.f_blocks) / (1024 * 1024 * 1024), 2)
        # Actual number of free bytes (Фактическое количество свободных байтов)
        # dv_statvfs_bfree = round((statvfs.f_frsize * statvfs.f_bfree) / (1024 * 1024 * 1024), 2)
        # Number of free bytes that ordinary users are allowed to use (excl. reserved space)
        # Количество свободных байтов, которые разрешено использовать обычным пользователям (исключая зарезервированное пространство)
        dv_statvfs_bavail = round((statvfs.f_frsize * statvfs.f_bavail) / (1024 * 1024 * 1024), 2)
        dv_result_bool = True
    except:
        pass
    return dv_statvfs_bavail, dv_statvfs_blocks, dv_result_bool


class Binlog2sql(object):

    #@snoop
    def __init__(self, connection_mysql_setting, connection_clickhouse_setting,
                 start_file=None, start_pos=None, end_file=None, end_pos=None,
                 start_time=None, stop_time=None, only_schemas=None, only_tables=None,
                 stop_never=False, back_interval=1.0, only_dml=True, sql_type=None, for_clickhouse=False,
                 log_id=None):
        """
        conn_mysql_setting: {'host': 127.0.0.1, 'port': 3306, 'user': user, 'passwd': passwd, 'charset': 'utf8'}
        """
        #
        if log_id is None:
            raise ValueError('no table "log_replication" in database ClickHouse or problems...')
        else:
            self.log_id = int(log_id)
        #
        self.conn_clickhouse_setting = connection_clickhouse_setting
        # dv_ch_client = Client(**self.conn_clickhouse_setting)
        # result = dv_ch_client.execute("SHOW DATABASES")
        # print(f"{result = }")
        #
        self.conn_mysql_setting = connection_mysql_setting
        #
        if not start_file:
            self.connection = pymysql.connect(**self.conn_mysql_setting)
            with self.connection.cursor() as cursor:
                cursor.execute("SET NAMES utf8")
                cursor.execute("SHOW MASTER LOGS")
                binlog_start_file = cursor.fetchone()[:1]
                self.start_file = binlog_start_file[0]
            # raise ValueError('Lack of parameter: start_file')
        else:
            self.start_file = start_file
        #
        self.start_pos = start_pos if start_pos else 4  # use binlog v4
        self.end_file = end_file
        self.end_pos = end_pos
        if start_time:
            self.start_time = datetime.datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
        else:
            self.start_time = datetime.datetime.strptime('1980-01-01 00:00:00', "%Y-%m-%d %H:%M:%S")
        if stop_time:
            self.stop_time = datetime.datetime.strptime(stop_time, "%Y-%m-%d %H:%M:%S")
        else:
            self.stop_time = datetime.datetime.strptime('2999-12-31 00:00:00', "%Y-%m-%d %H:%M:%S")
        #
        self.only_schemas = only_schemas if only_schemas else None
        self.only_tables = only_tables if only_tables else None
        self.stop_never, self.back_interval = (stop_never, back_interval)
        self.for_clickhouse = for_clickhouse
        # print(f"{self.for_clickhouse = }")
        self.only_dml = only_dml
        self.sql_type = [t.upper() for t in sql_type] if sql_type else []
        #
        self.binlogList = []
        self.connection = pymysql.connect(**self.conn_mysql_setting)
        with self.connection.cursor() as cursor:
            cursor.execute("SET NAMES utf8")
            cursor.execute("SHOW MASTER STATUS")
            self.eof_file, self.eof_pos = cursor.fetchone()[:2]
            if self.end_pos == 0:
                self.end_pos = self.eof_pos
            if self.end_file == '':
                self.end_file = self.eof_file
            #
            cursor.execute("SHOW MASTER LOGS")
            bin_index = [row[0] for row in cursor.fetchall()]
            if self.start_file not in bin_index:
                raise ValueError('parameter error: start_file %s not in mysql server' % self.start_file)
            binlog2i = lambda x: x.split('.')[1]
            for binary in bin_index:
                if binlog2i(self.start_file) <= binlog2i(binary) <= binlog2i(self.end_file):
                    self.binlogList.append(binary)
            # оставляем список только из количества файлов, которые разрешили для обработки за 1 раз (в settings.py)
            self.binlogList = self.binlogList[:settings.replication_max_number_files_per_session]
            #
            cursor.execute("SELECT @@server_id")
            self.server_id = cursor.fetchone()[0]
            if not self.server_id:
                raise ValueError(
                    'missing server_id in %s:%s' % (self.conn_mysql_setting['host'], self.conn_mysql_setting['port']))
            #
            # выводим список файлов бинлога, которые разрешили для обработки в этом запуске
            logger.debug(f"{self.server_id = }")
            logger.debug(f"{self.start_file = }")
            logger.debug(f"{self.start_pos = }")
            logger.debug(f"{self.end_file = }")
            logger.debug(f"{self.end_pos = }")
            logger.debug(f"{self.eof_file = }")
            logger.debug(f"{self.eof_pos = }")
            logger.debug(f"{self.binlogList = }")



    #@snoop
    def clear_binlog(self, log_time):
        logger.debug(f"clear_binlog")
        try:
            logger.debug(f" {log_time = }")
            tmp_LEAVE_BINARY_LOGS_IN_DAYS = datetime.datetime.today() - datetime.timedelta(
                days=settings.LEAVE_BINARY_LOGS_IN_DAYS)
            logger.debug(f" {tmp_LEAVE_BINARY_LOGS_IN_DAYS = }")
            if log_time > tmp_LEAVE_BINARY_LOGS_IN_DAYS:
                self.connection = pymysql.connect(**self.conn_mysql_setting)
                with self.connection.cursor() as cursor:
                    tmp_sql_execute = f"PURGE BINARY LOGS BEFORE DATE(NOW() - INTERVAL {settings.LEAVE_BINARY_LOGS_IN_DAYS} DAY) + INTERVAL 0 SECOND"
                    # print(f"{tmp_sql_execute = }")
                    if dv_EXECUTE_CLICKHOUSE is True:
                        cursor.execute(tmp_sql_execute)
                        logger.info(f"{tmp_sql_execute}")
        except Exception as ERROR:
            logger.error(f"{ERROR = }")

    #@snoop
    def del_old_row_from_mysql(self):
        logger.info(f"удаление старых записей из MySQL - begin")
        try:
            dv_txt_del_old = 'Старых записей для удаления не обнаружено'
            self.connection = pymysql.connect(**self.conn_mysql_setting)
            for dv_tbl_line in list(settings.CONST_TBL_FOR_DELETE_OLD.keys()):
                dv_query = settings.CONST_TBL_FOR_DELETE_OLD[dv_tbl_line].get('sql_get_max_id', '')
                # print(dv_query)
                if dv_query != '':
                    with self.connection.cursor() as cursor:
                        # cursor.execute("SET SESSION max_statement_time = 600")
                        cursor.execute(dv_query)
                        dv_id_max = cursor.fetchone()
                    if dv_id_max is not None:
                        # print(dv_id_max[0])
                        dv_query = settings.CONST_TBL_FOR_DELETE_OLD[dv_tbl_line].get('sql_count', '')
                        # print(dv_query)
                        if dv_query != '':
                            dv_query = dv_query.replace('{id_max}', f"{dv_id_max[0]}")
                            with self.connection.cursor() as cursor:
                                # cursor.execute("SET SESSION max_statement_time = 600")
                                cursor.execute(dv_query)
                                dv_row_count = cursor.fetchone()
                            if dv_row_count is not None:
                                # print(dv_row_count[0])
                                dv_query = settings.CONST_TBL_FOR_DELETE_OLD[dv_tbl_line].get('sql_delete', '')
                                # print(dv_query)
                                if dv_query != '':
                                    dv_query = dv_query.replace('{id_max}', f"{dv_id_max[0]}")
                                    logger.info(f"{dv_query}")
                                    with self.connection.cursor() as cursor:
                                        # cursor.execute("SET SESSION max_statement_time = 600")
                                        cursor.execute(dv_query)
                                    # Нужен коммит чтобы сохранилось удаление:
                                    self.connection.commit()
                                    dv_txt_del_old = f"УДАЛИЛИ из MySQL.matomo.{dv_tbl_line} {dv_row_count[0]} СТАРЫХ записей до id = {dv_id_max[0]}"
                                    del dv_query
                                    del dv_id_max
                                    del dv_row_count
                                    logger.info(f"{dv_txt_del_old}")
            logger.info(f"удаление старых записей из MySQL - end")
        except Exception as ERROR:
            logger.error(f"{ERROR = }")

    #@snoop
    def execute_in_clickhouse(self, dv_sql_4insert_dict={}):
        '''
        Функция обрабатывает полученный словарь, создает из него инсерты, выполняет на базе КликХауса, актуализирует и возвращает словарь
        '''
        dv_execute_time_begin = time.time()
        ch_execute_count = sum(map(len, dv_sql_4insert_dict.values()))
        dv_sql_4insert_dict = dict(dv_sql_4insert_dict)
        logger.debug(f"execute_in_clickhouse - {sum(map(len, dv_sql_4insert_dict.values())) = }")
        dv_sql_for_execute_last = ''
        if len(dv_sql_4insert_dict) != 0:
            # logger.debug(f"execute_in_clickhouse - {dv_sql_4insert_dict = }")
            with Client(**self.conn_clickhouse_setting) as ch_cursor:
                for dv_key, dv_value in dv_sql_4insert_dict.items():
                    if dv_key != '':
                        dv_sql_for_execute_all = dv_key
                        dv_sql_for_execute_last = f"{dv_sql_for_execute_all}{dv_sql_4insert_dict[dv_key][0]}"
                        for dv_value_i in range(0, len(dv_value)):
                            dv_sql_for_execute_all = f"{dv_sql_for_execute_all}{dv_sql_4insert_dict[dv_key][dv_value_i]}"
                        logger.debug(f"execute_in_clickhouse - {dv_sql_for_execute_all = }")
                        # выполняем строку sql
                        if dv_EXECUTE_CLICKHOUSE is True:
                            ch_cursor.execute(dv_sql_for_execute_all)
        logger.debug(f"execute_in_clickhouse - {dv_sql_for_execute_last = }")
        dv_sql_4insert_dict = {}
        ch_execute_work_time_ms = int('{:.0f}'.format(1000 * (time.time() - dv_execute_time_begin)))
        logger.info(f"execute_in_clickhouse: {ch_execute_count = } / {ch_execute_work_time_ms = }")
        return dv_sql_4insert_dict, dv_sql_for_execute_last

    #@snoop
    def process_binlog(self):
        logger.debug(f"process_binlog")
        dv_is_end_process_binlog = False
        dv_time_begin = time.time()
        dv_count_sql_for_ch = 0
        log_time = '1980-01-01 00:00:00'
        logger.debug(f"{self.server_id = }")
        logger.debug(f"{self.start_file = }")
        logger.debug(f"{self.start_pos = }")
        logger.debug(f"{self.only_schemas = }")
        logger.debug(f"{self.only_schemas = }")
        logger.debug(f"{self.only_tables = }")
        logger.debug(f"{self.only_dml = }")
        logger.debug(f"{self.sql_type = }")
        try:
            stream = BinLogStreamReader(connection_settings=self.conn_mysql_setting, server_id=self.server_id,
                                        log_file=self.start_file, log_pos=self.start_pos,
                                        only_schemas=self.only_schemas,
                                        only_tables=self.only_tables, resume_stream=True, blocking=True,
                                        # ignore_decode_errors=True,  # если раскомментировать, то могут появиться проблемы
                                        is_mariadb=False, freeze_schema=True)
            flag_last_event = False
            # dv_sql_for_execute_list = ''
            dv_sql_for_execute_last = ''
            dv_sql_log = ''
            # Создаем пустой словарь для инсертов в clickhouse
            dv_sql_4insert_dict = dict()
            e_start_pos, last_pos = stream.log_pos, stream.log_pos
            # to simplify code, we do not use flock for tmp_file.
            tmp_file = create_unique_file('%s.%s' % (self.conn_mysql_setting['host'], self.conn_mysql_setting['port']))
            with temp_open(tmp_file, "w") as f_tmp, self.connection.cursor() as cursor:
                cursor.execute("SET NAMES utf8")
                dv_tbl_not_delete = settings.CONST_TBL_NOT_DELETE_OLD
                for binlog_event in stream:
                    logger.debug(f"***")
                    logger.debug(f"for binlog_event in stream: {stream.log_file = }")
                    if not self.stop_never:
                        try:
                            event_time = datetime.datetime.fromtimestamp(binlog_event.timestamp)
                            logger.debug(f"{event_time = }")
                        except OSError:
                            event_time = datetime.datetime(1980, 1, 1, 0, 0)
                            logger.debug(f"ERROR {event_time = }")
                        if (stream.log_file == self.end_file and stream.log_pos == self.end_pos) or \
                                (stream.log_file == self.eof_file and stream.log_pos == self.eof_pos):
                            flag_last_event = True
                            logger.debug(f" for binlog_event in stream : {flag_last_event = }")
                        elif event_time < self.start_time:
                            if not (isinstance(binlog_event, RotateEvent)
                                    or isinstance(binlog_event, FormatDescriptionEvent)):
                                last_pos = binlog_event.packet.log_pos
                            logger.debug(f" for binlog_event in stream : {last_pos = }")
                            continue
                        elif (stream.log_file not in self.binlogList) or \
                                (self.end_pos and stream.log_file == self.end_file and stream.log_pos > self.end_pos) or \
                                (stream.log_file == self.eof_file and stream.log_pos > self.eof_pos) or \
                                (event_time >= self.stop_time):
                            logger.debug(f" for binlog_event in stream : break")
                            break
                        # else:
                        #     logger.warning(f"unknown binlog file or position")
                        #     # raise ValueError('unknown binlog file or position')
                        #     pass
                    #
                    if isinstance(binlog_event, QueryEvent) and binlog_event.query == 'BEGIN':
                        e_start_pos = last_pos
                        logger.debug(f" e_start_pos = last_pos : {e_start_pos = }")
                    #
                    if isinstance(binlog_event, QueryEvent) and not self.only_dml:
                        sql, log_pos_start, log_pos_end, log_shema, log_table, log_time, sql_type, sql_4insert_table, sql_4insert_values = concat_sql_from_binlog_event(
                            cursor=cursor,
                            binlog_event=binlog_event,
                            for_clickhouse=self.for_clickhouse)
                        if sql:
                            print(sql)
                    elif is_dml_event(binlog_event) and event_type(binlog_event) in self.sql_type:
                        for row in binlog_event.rows:
                            logger.debug(f"***")
                            logger.debug(f"{type(row) = }")
                            logger.debug(f"BEFORE: {row = }")
                            try:
                                for dv_row_key, dv_row_value in row['values'].items():
                                    if isinstance(row['values'][dv_row_key], str):
                                        logger.debug(f"***")
                                        logger.debug(f"{dv_row_key = }")
                                        logger.debug(f"BEFORE: {row['values'][dv_row_key] = }")
                                        row['values'][dv_row_key] = row['values'][dv_row_key].replace("'", r"\'")
                                        row['values'][dv_row_key] = row['values'][dv_row_key].replace('"', r'\"')
                                        logger.debug(f"AFTER: {row['values'][dv_row_key] = }")
                                        logger.debug(f"***")
                            except:
                                pass
                            logger.debug(f"AFTER: {row = }")
                            logger.debug(f"***")
                            #
                            # Вызываем обработчик для строки репликации (получим строку sql)
                            sql, log_pos_start, log_pos_end, log_shema, log_table, log_time, sql_type, sql_4insert_table, sql_4insert_values = concat_sql_from_binlog_event(
                                cursor=cursor,
                                binlog_event=binlog_event,
                                row=row,
                                e_start_pos=e_start_pos,
                                for_clickhouse=self.for_clickhouse)
                            #
                            logger.debug(f" {sql = }")
                            logger.debug(f" {sql_4insert_values = }")
                            logger.debug(f" {type(sql_4insert_values) = }")
                            #
                            # Проверяем даты для строк delete (если дата старая, то игнорируем эту строку и выполнять на итоговой БД не будем)
                            is_row_need = 1
                            try:
                                # Если тип строки "удаление", то будем проверять нужно удалять или нет
                                if sql_type == 'DELETE':
                                    dv_count_days = 0
                                    # Проверяем входит ли таблица из текущей строки в словарь исключенных из удаления таблиц.
                                    if binlog_event.table in list(dv_tbl_not_delete.keys()):
                                        dv_date = row['values'].get(dv_tbl_not_delete[binlog_event.table]['col_date'],
                                                                    datetime.datetime.now())
                                        dv_count_days = (datetime.datetime.now() - dv_date).days
                                    if dv_count_days > 31:
                                        # Если запрос хочет удалить данные, которым больше Х дней, то такой запрос выполнять на итоговой базе не будем.
                                        # Не удаляем старые данные из итоговой БД!
                                        is_row_need = 0
                                        # Считаем сколько попыток удаления отклонили
                                        dv_tbl_not_delete[binlog_event.table]['rejected count'] = dv_tbl_not_delete[
                                                                                                      binlog_event.table].get(
                                            'rejected count',
                                            0) + 1
                                        logger.debug(
                                            f"ОТКЛОНЕНО УДАЛЕНИЕ СТРОКИ (создана {dv_count_days} дней назад): {sql}")
                            except Exception as ERROR:
                                logger.error(f"Проверка даты для строк delete: {ERROR = }")
                                pass
                            if is_row_need == 0:
                                # Текущую строку заливать в итоговую базу данных НЕ БУДЕМ
                                logger.debug(f"Текущую строку заливать в итоговую базу данных НЕ БУДЕМ")
                                logger.debug(f"***")
                                pass
                            else:
                                # текущую строку заливать в итоговую базу данных БУДЕМ
                                #
                                logger.debug(f"***")
                                dv_count_sql_for_ch += 1
                                logger.debug(f" {dv_count_sql_for_ch = }")
                                if self.for_clickhouse is True:
                                    pass
                                else:
                                    sql += ' file %s' % (stream.log_file)
                                #
                                # print(dv_sql_log)
                                # print(sql)
                                # print(f"{log_shema = }")
                                # print(f"{log_table = }")
                                # print(f"{stream.log_file = }")
                                # print(f"{log_pos_start = }")
                                # print(f"{log_pos_end = }")
                                # print(f"{log_time = }")
                                logger.debug(f"execute sql to clickhouse | begin")
                                self.log_id += 1
                                dv_sql_log = "INSERT INTO `%s`.`log_replication` (`dateid`,`log_time`,`log_file`,`log_pos_start`,`log_pos_end`,`sql_type`)" \
                                             " VALUES (%s,'%s','%s',%s,%s,'%s');" \
                                             % (log_shema, get_dateid(), log_time, stream.log_file, int(log_pos_start),
                                                int(log_pos_end), sql_type)
                                #
                                if (dv_replication_batch_sql == 0) or (
                                        sql_type not in ('INSERT', 'INS-UPD')) or (
                                        len(sql.splitlines()) > 1) or (
                                        dv_find_text.search(sql) is not None):
                                    # сюда попадаем если в настройках указали обработку ПОСТРОЧНО
                                    # или тип запроса не из списка ('INSERT', 'INS-UPD')
                                    # или в запросе есть перевод каретки (СТРОК в запросе > 1)
                                    # или в запросе есть управляющий символ
                                    logger.info(
                                        f"LINE_BY_LINE: {(sql_type not in ('INSERT', 'INS-UPD')) = } | {(len(sql.splitlines()) > 1) = } | {(dv_find_text.search(sql) is not None) = }")
                                    # ВНИМАНИЕ!!! обязательно сначала надо применить предыдущие sql чтобы апдейты и удаления корректно сработали
                                    if len(dv_sql_4insert_dict) > 0:
                                        # попадаем сюда если dv_sql_4insert_dict не пустой
                                        logger.info(f"LINE_BY_LINE + BATCH | {len(dv_sql_4insert_dict) > 0 = }")
                                        dv_sql_4insert_dict, dv_sql_for_execute_last = self.execute_in_clickhouse(
                                            dv_sql_4insert_dict=dv_sql_4insert_dict)
                                    #
                                    # далее обрабатываем текущую строку
                                    with Client(**self.conn_clickhouse_setting) as ch_cursor:
                                        dv_sql_for_execute_last = sql
                                        logger.info(f"{dv_sql_for_execute_last = }")
                                        # выполняем строку sql
                                        if dv_EXECUTE_CLICKHOUSE is True:
                                            ch_cursor.execute(dv_sql_for_execute_last)
                                        #
                                        if dv_replication_batch_sql == 0:
                                            # если включена построчная запись, то будем писать лог для каждой строки
                                            dv_sql_for_execute_last = dv_sql_log
                                            dv_sql_log = ''
                                            logger.info(f"{dv_sql_for_execute_last = }")
                                            # выполняем строку sql
                                            if dv_EXECUTE_CLICKHOUSE is True:
                                                ch_cursor.execute(dv_sql_for_execute_last)
                                else:
                                    # пополняем список запросов и если требуется, то будем его обрабатывать
                                    # добавляем элемент в ключ словаря, если ключа нет, то сначала создаем его
                                    dv_sql_4insert_dict.setdefault(sql_4insert_table, []).append(sql_4insert_values)
                                    dv_count_values_from_dv_sql_4insert_dict = sum(
                                        map(len, dv_sql_4insert_dict.values()))
                                    if (dv_count_values_from_dv_sql_4insert_dict > dv_replication_batch_sql) or \
                                            (dv_count_sql_for_ch > dv_replication_batch_size):
                                        # попадаем сюда если в запрос собрали строк больше, чем replication_batch_sql
                                        # или обработали уже больше replication_batch_size запросов
                                        # (это нужно чтобы не слишком много съедать памяти)
                                        logger.info(
                                            f"BATCH: {(dv_count_values_from_dv_sql_4insert_dict >= dv_replication_batch_sql) = } | {(dv_count_sql_for_ch >= dv_replication_batch_size) = }")
                                        dv_sql_4insert_dict, dv_sql_for_execute_last = self.execute_in_clickhouse(
                                            dv_sql_4insert_dict=dv_sql_4insert_dict)
                                logger.debug(f"execute sql to clickhouse | end")
                    #
                    dv_f_work_munutes = round(int('{:.0f}'.format(1000 * (time.time() - dv_time_begin))) / (1000 * 60))
                    if not (isinstance(binlog_event, RotateEvent) or isinstance(binlog_event, FormatDescriptionEvent)):
                        last_pos = binlog_event.packet.log_pos
                    #
                    # если обработали заданное "максимальное количество запросов обрабатывать за один вызов", то прерываем цикл
                    if dv_count_sql_for_ch > dv_replication_batch_size:
                        dv_is_end_process_binlog = True
                        # break
                    # если обрабатывали дольше отведенного времени, то прерываем цикл
                    elif dv_f_work_munutes >= settings_replication_max_minutes:
                        dv_is_end_process_binlog = True
                        # break
                    elif flag_last_event:
                        dv_is_end_process_binlog = True
                        # break
                    #
                    if dv_is_end_process_binlog is True:
                        if len(dv_sql_4insert_dict) > 0:
                            # попадаем сюда если есть в словаре инсерты (теоретически сюда попадать не должны вообще, всё корректно должно отрабатывать при создании батчей)
                            logger.info(
                                f"BATCH: {(dv_is_end_process_binlog is True) = } | {len(dv_sql_4insert_dict) > 0 = }")
                            dv_sql_4insert_dict, dv_sql_for_execute_last = self.execute_in_clickhouse(
                                dv_sql_4insert_dict=dv_sql_4insert_dict)
                        #
                        if dv_sql_log != '':
                            # если есть что записать в лог, то запишем
                            with Client(**self.conn_clickhouse_setting) as ch_cursor:
                                dv_sql_for_execute_last = dv_sql_log
                                logger.info(f"SAVE_LOG: {(dv_sql_log != '') = }")
                                logger.info(f"{dv_sql_for_execute_last = }")
                                # выполняем строку sql
                                if dv_EXECUTE_CLICKHOUSE is True:
                                    ch_cursor.execute(dv_sql_for_execute_last)
                        # завершаем:
                        break
                #
                stream.close()
                f_tmp.close()
                #
                # Если из матомо удалили старую строку, а в кликхаусе её удалять не надо (см.логику), то выведем сообщение о количестве проигнорированных попыток удаления
                for dv_tbl_line in list(dv_tbl_not_delete.keys()):
                    if dv_tbl_not_delete[dv_tbl_line].get('rejected count', 0) > 0:
                        logger.info(
                            f"ОТКЛОНЕНО УДАЛЕНИЕ из таблицы ClickHouse {dv_tbl_line} СТАРЫХ строк: {dv_tbl_not_delete[dv_tbl_line].get('rejected count', 0)} шт.")
                #
                # чистим старые логи
                if settings.LEAVE_BINARY_LOGS_IN_DAYS > 0 and settings.LEAVE_BINARY_LOGS_IN_DAYS < 99:
                    try:
                        self.clear_binlog(log_time=log_time)
                    except Exception as ERROR:
                        logger.error(f"self.clear_binlog(log_time=log_time): {ERROR = }")
                #
                # удаляем старые записи из MySQL (чтобы база стала меньше размером)
                self.del_old_row_from_mysql()
        #
        except Exception as ERROR:
            f_status = 'ERROR'
            logger.error(f"{ERROR = }")
            if dv_sql_for_execute_last != '':
                f_text = f"'ERROR = LAST_SQL:\n\n{dv_sql_for_execute_last}\n\n{ERROR = }"
            else:
                f_text = f"{ERROR}"
        else:
            work_time_ms = f"{'{:.0f}'.format(1000 * (time.time() - dv_time_begin))}"
            f_text = f" = Успешно обработано {dv_count_sql_for_ch} строк за {work_time_ms} мс. | max_log_time = {log_time}"
            f_status = 'ERROR'
            try:
                # Здесь решаем нужно ли выполнять скрипты из settings.sql_execute_at_end_matomo2clickhouse
                if (settings_replication_max_minutes > 10) and (len(settings.sql_execute_at_end_matomo2clickhouse) > 0):
                    for dv_value_i in range(0, len(settings.sql_execute_at_end_matomo2clickhouse)):
                        dv_sql_for_execute_last = f"{settings.sql_execute_at_end_matomo2clickhouse[dv_value_i]}"
                        # если есть что выполнить, то выполняем
                        if dv_sql_for_execute_last != '':
                            with Client(**self.conn_clickhouse_setting) as ch_cursor:
                                logger.info(
                                    f"settings.sql_execute_at_end_matomo2clickhouse_{dv_value_i}: {dv_sql_for_execute_last = }")
                                # выполняем строку sql
                                if dv_EXECUTE_CLICKHOUSE is True:
                                    ch_cursor.execute(dv_sql_for_execute_last)
                                    pass
                    work_time_ms = int(f"{'{:.0f}'.format(1000 * (time.time() - dv_time_begin))}") - int(work_time_ms)
                    f_text = f"{f_text} | Время работы скрипта end = {work_time_ms} мс."
                    pass
                f_status = 'SUCCESS'
                f_text = f"{f_status}{f_text}"
            except Exception as ERROR:
                logger.error(f"{ERROR = }")
                if dv_sql_for_execute_last != '':
                    f_text = f"'ERROR = LAST_SQL:\n\n{dv_sql_for_execute_last}\n\n{ERROR = }"
                else:
                    f_text = f"{ERROR}"
        return f_status, f_text

    def __del__(self):
        pass


#@snoop
def get_ch_param_for_next(connection_clickhouse_setting):
    logger.debug(f"get_ch_param_for_next")
    log_id_max = -1
    log_time = '1980-01-01 00:00:00'
    log_file = ''
    log_pos_end = 0
    try:
        dv_ch_client = Client(**connection_clickhouse_setting)
        dv_ch_execute = dv_ch_client.execute(
            f"SELECT max(dateid) AS id_max FROM {settings.CH_matomo_dbname}.log_replication")
        log_id_max = dv_ch_execute[0][0]
        logger.debug(f"{log_id_max = }")
    except Exception as ERROR:
        logger.error(f"{ERROR = }")
        raise ERROR
    #
    try:
        ch_result = dv_ch_client.execute(
            f"SELECT dateid, log_time, log_file, log_pos_end FROM {settings.CH_matomo_dbname}.log_replication WHERE dateid={log_id_max} LIMIT 1")
        logger.debug(f"{ch_result = }")
        log_time = f"{ch_result[0][1].strftime('%Y-%m-%d %H:%M:%S')}"
        log_file = f"{ch_result[0][2]}"
        log_pos_end = int(ch_result[0][3])
    except:
        pass

    return log_id_max, log_time, log_file, log_pos_end


if __name__ == '__main__':
    dv_time_begin = time.time()
    # получаем информацию о свободном месте на диске в гигабайтах
    dv_disk_space_free_begin = get_disk_space()[0]
    logger.info(f"{dv_disk_space_free_begin = } Gb")
    #
    logger.info(f"{datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S.%f')}")
    dv_for_send_txt_type = ''
    dv_for_send_text = ''
    try:
        # читаем значения из конфига
        dv_lib_path_ini = f"{settings.PATH_TO_LIB}/matomo2clickhouse.cfg"
        dv_cfg = configparser.ConfigParser()
        if os.path.exists(dv_lib_path_ini):
            with open(dv_lib_path_ini, mode="r", encoding='utf-8') as fp:
                dv_cfg.read_file(fp)
        # читаем значения
        dv_cfg_last_send_tlg_success = dv_cfg.get('DEFAULT', 'last_send_tlg_success', fallback='2000-01-01 00:00:00')
        dv_cfg_last_run_is_success = dv_cfg.get('DEFAULT', 'last_run_is_success', fallback=1)
        logger.info(f"read cfg success")
    except:
        logger.error(f"read cfg error")
        pass
    logger.info(f"{dv_cfg_last_send_tlg_success = }")
    logger.info(f"{dv_cfg_last_run_is_success = }")
    #
    # если последний запуск завершился ошибкой, то принудительно будем выполнять все запросы по одному, а количество запросов уменьшим
    if dv_cfg_last_run_is_success == '0':
        dv_replication_batch_sql = 0
        dv_replication_batch_size = (settings.replication_batch_size // 1000) + 10
    else:
        dv_replication_batch_sql = settings.replication_batch_sql
        dv_replication_batch_size = settings.replication_batch_size
    logger.info(f"{dv_replication_batch_sql = }")
    logger.info(f"{dv_replication_batch_size = }")
    #
    try:
        dv_file_lib_path = f"{settings.PATH_TO_LIB}/matomo2clickhouse.dat"
        if os.path.exists(dv_file_lib_path):
            dv_file_lib_open = open(dv_file_lib_path, mode="r", encoding='utf-8')
            dv_file_lib_time = next(dv_file_lib_open).strip()
            dv_file_lib_open.close()
            dv_file_old_start = datetime.datetime.strptime(dv_file_lib_time, '%Y-%m-%d %H:%M:%S')
            tmp_now = datetime.datetime.strptime(
                datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S'), '%Y-%m-%d %H:%M:%S')
            tmp_seconds = int((tmp_now - dv_file_old_start).total_seconds())
            if tmp_seconds < settings_replication_max_minutes * 2 * 60:
                raise Exception(
                    f"Уже выполняется c {dv_file_lib_time} - перед запуском дождитесь завершения предыдущего процесса!")
        else:
            dv_file_lib_open = open(dv_file_lib_path, mode="w", encoding='utf-8')
            dv_file_lib_time = f"{datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')}"
            dv_file_lib_open.write(f"{dv_file_lib_time}")
            dv_file_lib_open.close()
        #
        # если скрипт вызвали с параметрами, то будем обрабатывать параметры, если без параметров, то возьмем предустановленные параметры из settings.py
        if sys.argv[1:] != []:
            # print(f"{sys.argv[1:] = }")
            in_args = sys.argv[1:]
        else:
            # print(f"{settings.args_for_mysql_to_clickhouse = }")
            in_args = settings.args_for_mysql_to_clickhouse[1:]
        # parse args
        args = command_line_args(in_args)
        #
        if dv_SSH_MySQL_CONNECT is True:
            # Стартуем SSH-туннель
            tunnel = create_ssh_tunnel()
            conn_mysql_setting = {'host': args.host, 'port': tunnel.local_bind_port,
                                  'user': args.user, 'passwd': args.password, 'charset': 'utf8mb4'}
            logger.info(f"SSH {tunnel.local_bind_port = }")
        else:
            # conn_mysql_setting = {'host': args.host, 'port': args.port, 'user': args.user, 'passwd': args.password, 'charset': 'utf8'}
            conn_mysql_setting = {'host': args.host, 'port': args.port,
                                  'user': args.user, 'passwd': args.password, 'charset': 'utf8mb4'}
        conn_clickhouse_setting = settings.CH_connect
        #
        log_id = 0
        if args.start_file == '':
            log_id, log_time, log_file, log_pos_end = get_ch_param_for_next(
                connection_clickhouse_setting=conn_clickhouse_setting)
            if log_file != '':
                args.start_file = log_file
                args.start_pos = log_pos_end
                args.start_time = log_time
        try:
            logger.info(f"binlog_datetime_start = {args.start_time}")
        except:
            pass
        binlog2sql = Binlog2sql(connection_mysql_setting=conn_mysql_setting,
                                connection_clickhouse_setting=conn_clickhouse_setting,
                                start_file=args.start_file, start_pos=args.start_pos,
                                end_file=args.end_file, end_pos=args.end_pos,
                                start_time=args.start_time, stop_time=args.stop_time,
                                only_schemas=args.databases, only_tables=args.tables,
                                stop_never=args.stop_never,
                                back_interval=args.back_interval, only_dml=args.only_dml,
                                sql_type=args.sql_type, for_clickhouse=args.for_clickhouse,
                                log_id=log_id)
        dv_for_send_txt_type, dv_for_send_text = binlog2sql.process_binlog()
        #
        # если заливка данных в кликхаус прошла с ошибкой, то делаем отметку об этом
        if dv_for_send_txt_type == 'SUCCESS':
            dv_cfg_last_run_is_success = '1'
        else:
            dv_cfg_last_run_is_success = '0'
        #
        os.remove(dv_file_lib_path)
    except Exception as ERROR:
        logger.error(f"{ERROR = }")
        dv_for_send_txt_type = 'ERROR'
        dv_for_send_text = f"{ERROR = }"
    finally:
        # сохраняем информацию о результате работы скрипта (успешно/ошибка)
        try:
            dv_cfg.set('DEFAULT', 'last_run_is_success', dv_cfg_last_run_is_success)
        except Exception as ERROR:
            logger.error(f"{ERROR = }")
            pass
        # получаем информацию о свободном месте на диске в гигабайтах
        dv_disk_space_free_end, dv_statvfs_blocks, dv_result_bool = get_disk_space()
        logger.info(f"{dv_disk_space_free_end = } Gb")
        try:
            if settings.CHECK_DISK_SPACE is True:
                # формируем текст о состоянии места на диске
                if dv_result_bool is True:
                    dv_for_send_text = f"{dv_for_send_text} | disk space all/free_begin/free_end: {dv_statvfs_blocks}/{dv_disk_space_free_begin}/{dv_disk_space_free_end} Gb"
                else:
                    dv_for_send_text = f"{dv_for_send_text} | check_disk_space = ERROR"
        except:
            pass
        #
        if dv_for_send_txt_type == 'ERROR':
            logger.error(f"{dv_for_send_text}")
        else:
            logger.info(f"{dv_for_send_text}")
        try:
            if settings.SEND_TELEGRAM is True:
                if dv_for_send_txt_type == 'ERROR':
                    # если отработало с ошибкой, то в телеграм оправляем ошибку ВСЕГДА! хоть каждую минуту - это не спам
                    dv_is_SEND_TELEGRAM_success = True
                else:
                    dv_is_SEND_TELEGRAM_success = False
                    # Чтобы слишком часто не спамить в телеграм сначала проверим разрешено ли именно сейчас отправлять сообщение
                    try:
                        # проверяем нужно ли отправлять успех в телеграм
                        if get_second_between_now_and_datetime(
                                dv_cfg_last_send_tlg_success) > settings.SEND_SUCCESS_REPEATED_NOT_EARLIER_THAN_MINUTES * 60:
                            dv_is_SEND_TELEGRAM_success = True
                            # актуализируем значение конфига
                            dv_cfg_last_send_tlg_success = get_now()
                            dv_cfg.set('DEFAULT', 'last_send_tlg_success', dv_cfg_last_send_tlg_success)
                        else:
                            dv_is_SEND_TELEGRAM_success = False
                    except:
                        dv_is_SEND_TELEGRAM_success = False
                #
                # пытаться отправить будем только если предыдущие проверки подтвердили необходимость отправки
                if dv_is_SEND_TELEGRAM_success is True:
                    settings.f_telegram_send_message(tlg_bot_token=settings.TLG_BOT_TOKEN,
                                                     tlg_chat_id=settings.TLG_CHAT_FOR_SEND,
                                                     txt_name=f"matomo2clickhouse {dv_file_version}",
                                                     txt_type=dv_for_send_txt_type,
                                                     txt_to_send=f"{dv_for_send_text}",
                                                     txt_mode=None)
                    logger.info(f"send to telegram: {dv_for_send_text}")
        except:
            pass
        try:
            # сохраняем файл конфига
            with open(dv_lib_path_ini, mode='w', encoding='utf-8') as configfile:
                dv_cfg.write(configfile)
        except:
            pass
        try:
            # получаем позицию binlog-а, до которой обработали (для статистики)
            log_id, dv_binlog_datetime_end, log_file, log_pos_end = get_ch_param_for_next(
                connection_clickhouse_setting=conn_clickhouse_setting)
            logger.info(f"binlog_datetime_end = {dv_binlog_datetime_end}")
        except:
            pass
        #
        logger.info(f"{datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S.%f')}")
        work_time_ms = int('{:.0f}'.format(1000 * (time.time() - dv_time_begin)))
        logger.info(f"{work_time_ms = }")
        #
        logger.info(f'END')
