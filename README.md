# matomo2clickhouse

Replication Matomo from MySQL to ClickHouse (Репликация Matomo: переливка данных из MySQL в ClickHouse)

Для работы потребуется Linux (тестирование проводилось на ubuntu 22.04.01). При необходимости переписать под windows, вероятно, вам не составит большого труда.

Сначала всё настроить, потом вручную запускать проект: ```matomo2clickhouse_start.sh```

Для автоматизации можно настроить (например, через cron) выполнение скрипта ```matomo2clickhouse.py```

### Кратко о том как устроена работа matomo2clickhouse:

- MySQL делает репликацию (пишет binlog со всеми запросами, которые выполняются в базе данных).
- При запуске matomo2clickhouse читает репликацию (настройки и их описания содержатся в settings.py) с момента предыдущей остановки, преобразует sql-запросы
  выбранных таблиц в формат для ClickHouse и выполняет эти запросы в ClickHouse.
- В таблице ClickHouse.log_replication ведется логирование: какая позиция бинлога успешно записана в ClickHouse (соответственно по записям можно понять что
  загружено и во сколько). Именно этой таблицей пользуется при запуске matomo2clickhouse чтобы понять с какого места бинлога продолжать переливать данные в
  ClickHouse.

### Кратко весь процесс настройки:

- Создать таблицы в ClickHouse (для создания таблиц выполнить скрипт из проекта с учетом своих настроек)
- Скопировать все уже существующие данные из MySQL в ClickHouse (самостоятельно любым способом)
- Настроить репликацию из MySQL в ClickHouse (настроить как описано в текущей инструкции, но с учётом особенностей вашей системы)

***ВНИМАНИЕ! Пути до каталогов и файлов везде указывайте свои!***

### MySQL

- Matomo может использовать MySQL/MariaDB/Percona или другие DB семейства MySQL, далее будем это всё называть MySQL
- Для работы python с MySQL скорее всего сначала потребуется установить клиентскую библиотеку для ОС, поэтому пробуем установить:

```sudo apt install libmysqlclient-dev```

- Для работы репликации в MySQL нужно включить binlog. Внимание: необходимо предусмотреть чтобы было достаточно места на диске для бинлога!

```
Редактируем /etc/mysql/mariadb.conf.d/50-server.cnf (файл может быть в другом месте):

[mysqld]:
default-authentication-plugin = mysql_native_password
server_id = 1
log_bin = /var/log/mysql/mysql-bin.log
max_binlog_size = 100M
expire_logs_days = 30
binlog_format = row
binlog_row_image = full
binlog_do_db = название базы, (можно несколько строк для нескольких баз)
```

- После внесенных изменений рестартуем сервис БД (название сервиса может отличаться):

```sudo systemctl restart mariadb.service```

- В базе MySQL завести пользователя и задать ему права:

```GRANT SELECT, PROCESS, SUPER, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'user'@'%';```

### ClickHouse

- Для создания структуры выполнить скрипт: script_create_clickhouse_table.sql (ВНИМАНИЕ!!! сначала необходимо изучить скрипт!)
- Если потребуются дополнительные таблицы, то читать описание внутри _settings.py

### Установка matomo2clickhouse (выполняем пошагово)

- Устанавливаем python (тестирование данной инструкции проводилось на 3.10, на остальных версиях работу не гарантирую, но должно работать на версиях 3.9+, если
  вам потребуется, то без особенного труда сможете переписать даже под 2.7)
- Устанавливаем pip:

```sudo apt install python3-pip```

- Далее устанавливаем pipenv (на linux):

```pip3 install pipenv```

- Создаем нужный каталог в нужном нам месте
- Копируем в этот каталог файлы проекта https://github.com/dneupokoev/matomo2clickhouse
- Заходим в созданный каталог и создаем в нем пустой каталог .venv
- В каталоге проекта выполняем команды (либо иным способом устанавливаем пакеты из requirements.txt):

```pipenv shell```

```pipenv sync```

- Редактируем и переименовываем файл _settings.py (описание внутри файла)
- Настраиваем регулярное выполнение (например, через cron) скрипта:

```matomo2clickhouse.py```

### Дополнительно

- Обратите внимание на описание внутри _settings.py - там все настройки
- Если работает экземпляр программы, то второй экземпляр запускаться не будет (отслеживается через создание и проверку наличия файла)
- Записывается лог ошибок (настраивается в settings, рекомендуется сюда: /var/log/matomo2clickhouse/)
- Если задать нужные настройки в settings, то результат работы будет присылать в телеграм (в личку или указанный канал)
- Можно включить (в settings) вывод информации о дисковом пространстве

### Добавления задания в cron

Смотрим какие задания уже созданы для данного пользователя:

```crontab -l```

Открываем файл для создания задания:

```crontab -e```

Каждая задача формируется следующим образом (для любого значения нужно использовать звездочку "*"):

```минута(0-59) час(0-23) день(1-31) месяц(1-12) день_недели(0-7) /полный/путь/к/команде```

Чтобы matomo2clickhouse запускался каждый час ровно в 7 минут, создаем строку и сохраняем файл:

```7 */1 * * * /opt/dix/matomo2clickhouse/matomo2clickhouse_cron.sh```

ВНИМАНИЕ!!! отредактируйте содержимое файла matomo2clickhouse_cron.sh и сделайте его исполняемым

### Возможные проблемы и их решение

- Всё установили и запускам, но получаем ошибку ```unknown encoding: utf8mb3```, скорее всего можно починить примерно так:

```
cd /usr/lib/python3.10/encodings
cp utf_8.py utf8mb3.py
```

- При ошибке ```'utf-8' codec can't decode bytes in position 790-791: unexpected end of data```
  помогло добавление параметра ```errors="ignore"```
  в ```.venv/lib/python3.10/site-packages/pymysqlreplication/events.py```
  строка 203 в параметр ```.decode("utf-8", errors="ignore")```:
  ```self.query = self.packet.read(event_size - 13 - self.status_vars_length - self.schema_length - 1).decode("utf-8", errors="ignore")```

### ВНИМАНИЕ!

- В переменной ```settings.tables_not_updated``` указаны таблицы, для которых все UPDATE заменены на INSERT, т.е. записи добавляются, а не изменяются. Это
  необходимо учитывать при селектах! Актуальные записи - те, у которых максимальное значение ```dateid```.
  Сделано это для того, чтобы ClickHouse работал корректно (он не заточен на UPDATE - это ОЧЕНЬ медленная операция)
- Для примера (как получать актуальные данные) созданы 2 представления: ```view_matomo_log_visit``` и```view_matomo_log_link_visit_action```
- Базы данных (схемы) должны называться одинаково в MySQL и ClickHouse. Поскольку в скрипте ```script_create_clickhouse_table.sql``` база называется "matomo",
  то и базы должны называться "matomо", либо необходимо поправить название в скрипте. Возможно в следующих версиях будет реализовано разное название баз данных,
  но пока так.
- Перед обновлением matomo НЕОБХОДИМО (!!!) изучить что меняется. В случае, если меняется структура базы данных, то нужно проработать план обновления (перед
  обновой сначала провести полный обмен с остановкой базы данных matomo, после этого привести стуртуру баз данных в соотвествите и т.д.)
- За один запуск matomo2clickhouse переливает не все бинлоги, а только то, что вы настроите в ```settings.py```
- matomo2clickhouse не переливает все таблицы и всё их содержимое, а читает настройки в settings.py, ищет последнюю позицию в
  ```ClickHouse.log_replication.log_time``` и пишет только то, что появляется новое в бинлогах. То есть если вы сегодня включили репликацию, то данные будут
  переливаться только с этого момента. Если в какой-то момент вы удалите файл репликации, а он нужен matomo2clickhouse, то переливка остановится с ошибкой.
  Если вы полностью очистите таблицу ```ClickHouse.log_replication```, то будут переливаться все имеющиеся бинлоги, не зависимо от того переливались ли они
  ранее.
