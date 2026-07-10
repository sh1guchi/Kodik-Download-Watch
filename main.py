from flask import Flask, render_template, request, redirect, abort, session, send_file, send_from_directory, g
from flask_socketio import SocketIO, send, emit, join_room, leave_room
from flask_mobility import Mobility
from getters import *
from fast_download import clear_tmp, fast_download, get_path
import batch_download
import watch_together
from json import load
import config
import os

app = Flask(__name__)
Mobility(app)
socketio = SocketIO(app)

token = config.KODIK_TOKEN
app.config['SECRET_KEY'] = config.APP_SECRET_KEY

with open("translations.json", 'r', encoding='utf-8') as f:
    # Используется для указания озвучки при скачивании файла
    translations = load(f)

ch = None
if config.USE_SAVED_DATA or config.SAVE_DATA:
    from cache import Cache
    ch = Cache(config.SAVED_DATA_FILE, config.SAVING_PERIOD, config.CACHE_LIFE_TIME)
ch_save = config.SAVE_DATA
ch_use = config.USE_SAVED_DATA

watch_manager = watch_together.Manager(config.REMOVE_TIME)

# Очистка tmp
clear_tmp()

# Проверка доступности шикимори
test_shiki()


# --------------------------------------------------------------- Хелперы
def is_dark() -> bool:
    return session.get('is_dark', False)


def cached_title(serv: str, id: str):
    """Название тайтла из кеша или None. get_data_by_id кидает KeyError."""
    if not (ch_use and ch):
        return None
    try:
        data = ch.get_data_by_id(serv + id)
        return data['title'] if data else None
    except KeyError:
        return None


def resolve_link(serv: str, id: str, translation_id: str, seria: int) -> str:
    """
    Единая точка получения ссылки на серию: сперва кеш, затем Kodik.
    Раньше этот блок был скопирован в 4 местах (download/watch/room).
    """
    if serv == "sh":
        id_type = "shikimori"
    elif serv == "kp":
        id_type = "kinopoisk"
    else:
        raise ValueError(f"Неизвестный источник: {serv}")
    key = serv + id
    if ch_use and ch and ch.is_seria(key, translation_id, seria):
        return ch.get_seria(key, translation_id, seria)
    url = get_download_link(id, id_type, seria, translation_id, token)
    if ch_save and ch:
        try:
            ch.add_seria(key, translation_id, seria, url)
        except KeyError:
            pass  # тайтла ещё нет в кеше — не страшно
    return url


def parse_data(data: str):
    """'start:end-translation_id' -> ([start, end], translation_id)"""
    parts = data.split('-')
    series = [int(x) for x in parts[0].split(':')]
    if len(series) == 1:
        series = [1, series[0]]
    translation_id = str(parts[1])
    return series, translation_id


# --------------------------------------------------------------- Страницы
@app.route('/')
def index():
    return render_template('index.html', is_dark=is_dark(), is_kodik_search=USE_KODIK_SEARCH)


@app.route('/', methods=['POST'])
def index_form():
    data = dict(request.form)
    if 'shikimori_id' in data:
        return redirect(f"/download/sh/{data['shikimori_id'].strip()}/")
    if 'kinopoisk_id' in data:
        return redirect(f"/download/kp/{data['kinopoisk_id'].strip()}/")
    if 'kdk' in data:  # kdk = Kodik
        return redirect(f"/search/kdk/{data['kdk'].strip()}/")
    return abort(400)


@app.route("/api/theme/", methods=['POST'])
def api_theme():
    # Синхронизация темы (основной источник правды — localStorage на клиенте,
    # сессия нужна, чтобы первая серверная отрисовка совпадала с клиентской)
    body = request.get_json(silent=True) or {}
    session['is_dark'] = body.get('theme') == 'dark'
    return {'ok': True}


@app.route("/change_theme/", methods=['POST'])
def change_theme():
    # Старый способ смены темы, оставлен для совместимости
    session['is_dark'] = not session.get('is_dark', False)
    return redirect(request.referrer or '/')


@app.route('/search/<string:db>/<string:query>/')
def search_page(db, query):
    if db != "kdk":
        # Другие базы не поддерживаются (возможно в будущем будут)
        return abort(400)
    try:
        s_data = get_search_data(query, token, ch if (ch_save or ch_use) else None)
        return render_template('search.html', items=s_data[0], others=s_data[1],
                               is_dark=is_dark(), is_mobile=g.is_mobile, is_kodik_search=USE_KODIK_SEARCH)
    except Exception as ex:
        if config.DEBUG:
            print(f"[SEARCH] error: {ex}")
        return render_template('search.html', items=[], others=[],
                               is_dark=is_dark(), is_mobile=g.is_mobile, is_kodik_search=USE_KODIK_SEARCH)


@app.route('/download/<string:serv>/<string:id>/')
def download_shiki_choose_translation(serv, id):
    if serv == "sh":
        key = "sh" + id
        serial_data = None
        if ch_use and ch:
            try:
                cached = ch.get_data_by_id(key)
                if cached and cached.get('serial_data'):
                    serial_data = cached['serial_data']
            except KeyError:
                pass
        if serial_data is None:
            try:
                # Данные о наличии переводов от кодика
                serial_data = get_serial_info(id, "shikimori", token)
            except Exception as ex:
                return render_template('not_found.html', is_dark=is_dark(),
                                       details=str(ex) if config.DEBUG else None), 404

        cache_used = False
        name = pic = score = dtype = date = status = rating = description = 'Неизвестно'
        year = 1970
        if ch_use and ch:
            try:
                cached = ch.get_data_by_id(key)
                name, pic, score = cached['title'], cached['image'], cached['score']
                dtype, date, status = cached['type'], cached['date'], cached['status']
                rating, year, description = cached['rating'], cached['year'], cached['description']
                # При поиске по шики картинки урезанные — проверяем качество
                cache_used = is_good_quality_image(pic)
            except KeyError:
                pass
        if not cache_used:
            data = False
            try:
                data = get_shiki_data(id)
                name, pic, score = data['title'], data['image'], data['score']
                dtype, date, status = data['type'], data['date'], data['status']
                rating, year, description = data['rating'], data['year'], data['description']
            except Exception:
                name = 'Неизвестно'
                pic = config.IMAGE_NOT_FOUND
                score = dtype = date = status = rating = description = 'Неизвестно'
                year = 1970
            if ch_save and ch and not ch.is_id(key):
                ch.add_id(key, name, pic, score,
                          data['status'] if data else "Неизвестно",
                          data['date'] if data else "Неизвестно",
                          data['year'] if data else 1970,
                          data['type'] if data else "Неизвестно",
                          data['rating'] if data else "Неизвестно",
                          data['description'] if data else '',
                          serial_data=serial_data)
        if ch_use and ch_save and ch and ch.is_id(key):
            try:
                if ch.get_data_by_id(key).get('serial_data') in ({}, None):
                    ch.add_serial_data(key, serial_data)
            except KeyError:
                pass

        related = []
        try:
            if ch_use and ch and ch.is_id(key) and ch.get_data_by_id(key).get('related'):
                related = ch.get_data_by_id(key)['related']
            else:
                related = get_related(id, 'shikimori', sequel_first=True)
                if ch_save and ch:
                    try:
                        ch.add_related(key, related)
                    except KeyError:
                        pass
        except Exception:
            related = []

        return render_template('info.html',
            title=name, image=pic, score=score,
            translations=serial_data['translations'], series_count=serial_data["series_count"], id=id,
            dtype=dtype, date=date, status=status, rating=rating, related=related,
            description=description, is_shiki=True,
            is_dark=is_dark(), is_mobile=g.is_mobile,
            shiki_mirror=config.SHIKIMORI_MIRROR if config.SHIKIMORI_MIRROR else "shikimori.one")

    elif serv == "kp":
        try:
            serial_data = get_serial_info(id, "kinopoisk", token)
        except Exception as ex:
            return render_template('not_found.html', is_dark=is_dark(),
                                   details=str(ex) if config.DEBUG else None), 404
        return render_template('info.html',
            title=f"Кинопоиск #{id}", image=config.IMAGE_NOT_FOUND, score=None,
            translations=serial_data['translations'], series_count=serial_data["series_count"], id=id,
            dtype="Неизвестно", date="Неизвестно", status="Неизвестно", rating=None,
            related=[], description='', is_shiki=False,
            is_dark=is_dark(), is_mobile=g.is_mobile)
    return abort(400)


@app.route('/download/<string:serv>/<string:id>/<string:data>/')
def download_choose_seria(serv, id, data):
    if data == "None":
        return abort(404)
    try:
        series, _ = parse_data(data)
    except (IndexError, ValueError):
        return abort(400)
    return render_template('download.html', series=series, serv=serv, id=id, data=data,
                           backlink=f"/download/{serv}/{id}/",
                           is_dark=is_dark(), is_mobile=g.is_mobile)


@app.route('/download/<string:serv>/<string:id>/<string:data>/<string:download_type>-<string:quality>-<int:seria>/')
def redirect_to_download(serv, id, data, download_type, quality, seria):
    try:
        series, translation_id = parse_data(data)
    except (IndexError, ValueError):
        return abort(400)
    if download_type == 'fast':
        return redirect(f'/fast_download/{serv}-{id}-{seria}-{translation_id}-{quality}-{series[1]}/')
    try:
        url = resolve_link(serv, id, translation_id, seria)
    except ValueError:
        return abort(400)
    except Exception as ex:
        return abort(500, f'Не удалось получить ссылку: {ex}')
    translation = translations.get(translation_id, "Неизвестно")
    if seria == 0:
        return redirect(f"https:{url}{quality}.mp4:Перевод-{translation}:.mp4")
    return redirect(f"https:{url}{quality}.mp4:Серия-{seria}:Перевод-{translation}:.mp4")


@app.route('/download/<string:serv>/<string:id>/<string:data>/watch-<int:num>/')
def redirect_to_player(serv, id, data, num):
    try:
        series, _ = parse_data(data)
    except (IndexError, ValueError):
        return abort(400)
    if series[0] == 0 and series[1] == 0:
        return redirect(f'/watch/{serv}/{id}/{data}/0/')
    return redirect(f'/watch/{serv}/{id}/{data}/{num}/')


# --------------------------------------------------------------- Просмотр
@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/<string:old_quality>/q-<string:quality>/')
@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/<string:old_quality>/<int:timing>/q-<string:quality>/')
def change_watch_quality(serv, id, data, seria, old_quality, quality, timing=None):
    return redirect(f"/watch/{serv}/{id}/{data}/{seria}/{quality}/{str(timing)+'/' if timing else ''}")


@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/q-<string:quality>/')
@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/q-<string:quality>/<int:timing>/')
def redirect_to_old_type_quality(serv, id, data, seria, quality, timing=0):
    return redirect(f"/watch/{serv}/{id}/{data}/{seria}/{quality}/{str(timing)+'/' if timing else ''}")


@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/')
@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/<string:quality>/')
@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/<string:quality>/<int:timing>/')
def watch(serv, id, data, seria, quality="720", timing=0):
    try:
        series, translation_id = parse_data(data)
        title = cached_title(serv, id)
        url = resolve_link(serv, id, translation_id, seria)
    except ValueError:
        return abort(400)
    except Exception as ex:
        if config.DEBUG:
            print(f"[WATCH] error: {ex}")
        return abort(404)
    straight_url = f"https:{url}{quality}.mp4"  # Прямая ссылка
    dl_url = f"/download/{serv}/{id}/{data}/old-{quality}-{seria}"  # Скачивание через сервер
    return render_template('watch.html',
        url=dl_url, seria=seria, series=series, id=id, data=data, quality=quality,
        serv=serv, straight_url=straight_url,
        allow_watch_together=config.ALLOW_WATCH_TOGETHER,
        is_dark=is_dark(), timing=timing, title=title, is_mobile=g.is_mobile)


@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/', methods=['POST'])
@app.route('/watch/<string:serv>/<string:id>/<string:data>/<int:seria>/<string:quality>/', methods=['POST'])
def change_seria(serv, id, data, seria, quality=None):
    # Форма «перейти к серии»
    try:
        new_seria = int(dict(request.form)['seria'])
        series, _ = parse_data(data)
    except (KeyError, ValueError, IndexError):
        return abort(400)
    if not (series[0] <= new_seria <= series[1]):
        return abort(400, "Данная серия не существует")
    return redirect(f"/watch/{serv}/{id}/{data}/{new_seria}/{quality+'/' if quality else ''}")


# --------------------------------------------------------- Watch Together
@app.route('/create_room/', methods=['POST'])
def create_room():
    orig = request.referrer
    if not orig:
        return abort(400)
    parts = orig.split("/")
    if len(parts) == 9:
        parts[8] = '720'
        parts.append('')
    temp = parts[-4].split('-')
    try:
        # temp[0] может быть '1:12' (диапазон) или '12' (старый формат)
        series_count = int(temp[0].split(':')[-1])
        data = {
            'serv': parts[-6],
            'id': parts[-5],
            'series_count': series_count,
            'translation_id': temp[1],
            'seria': int(parts[-3]),
            'quality': int(parts[-2]),
            'pause': False,
            'play_time': 0,
        }
    except (IndexError, ValueError):
        return abort(400)
    rid = watch_manager.new_room(data)
    watch_manager.remove_old_rooms()
    return redirect(f"/room/{rid}/")


@app.route('/room/<string:rid>/', methods=["GET"])
def room(rid):
    if not watch_manager.is_room(rid):
        return abort(404)
    rd = watch_manager.get_room_data(rid)
    watch_manager.room_used(rid)
    try:
        id = rd['id']
        seria = rd['seria']
        series = rd['series_count']
        translation_id = str(rd['translation_id'])
        quality = rd['quality']
        url = resolve_link(rd['serv'], id, translation_id, seria)
        id_type = "shikimori" if rd['serv'] == "sh" else "kinopoisk"
        straight_url = f"https:{url}{quality}.mp4"
        dl_url = f"/download/{rd['serv']}/{id}/{series}-{translation_id}/{quality}-{seria}"
        return render_template('room.html',
            url=dl_url, seria=seria, series=series, id=id, id_type=id_type,
            data=f"{series}-{translation_id}", quality=quality, serv=rd['serv'],
            straight_url=straight_url, start_time=rd['play_time'],
            is_dark=is_dark(), is_mobile=g.is_mobile)
    except Exception as ex:
        if config.DEBUG:
            print(f"[ROOM] error: {ex}")
        return abort(500)


@app.route('/room/<string:rid>/', methods=["POST"])
def change_room_seria_form(rid):
    if not watch_manager.is_room(rid):
        return abort(404)
    try:
        new_seria = int(dict(request.form)['seria'])
    except (KeyError, ValueError):
        return redirect(f"/room/{rid}/")
    rdata = watch_manager.get_room_data(rid)
    if 1 <= new_seria <= rdata['series_count']:
        rdata['seria'] = new_seria
        rdata['play_time'] = 0
        watch_manager.room_used(rid)
        socketio.send({"data": {"status": 'update_page', 'time': 0}}, to=rid)
    return redirect(f"/room/{rid}/")


@app.route('/room/<string:rid>/cs-<int:seria>/')
def change_room_seria(rid, seria):
    if not watch_manager.is_room(rid):
        return abort(400)
    rdata = watch_manager.get_room_data(rid)
    rdata['seria'] = seria
    rdata['play_time'] = 0
    watch_manager.room_used(rid)
    socketio.send({"data": {"status": 'update_page', 'time': 0}}, to=rid)
    return redirect(f"/room/{rid}/")


@app.route('/room/<string:rid>/cq-<int:quality>/')
def change_room_quality(rid, quality):
    if not watch_manager.is_room(rid):
        return abort(400)
    rdata = watch_manager.get_room_data(rid)
    rdata['quality'] = quality
    watch_manager.room_used(rid)
    socketio.send({"data": {"status": 'update_page', 'time': rdata['play_time']}}, to=rid)
    return redirect(f"/room/{rid}/")


# --------------------------------------------------------- Быстрая загрузка
@app.route('/fast_download_act/<string:id_type>-<string:id>-<int:seria_num>-<string:translation_id>-<string:quality>/')
@app.route('/fast_download_act/<string:id_type>-<string:id>-<int:seria_num>-<string:translation_id>-<string:quality>-<int:max_series>/')
def fast_download_work(id_type: str, id: str, seria_num: int, translation_id: str, quality: str, max_series: int = 12):
    translation = translations.get(translation_id, "Неизвестно")
    add_zeros = len(str(max_series))
    serv = id_type if id_type in ("sh", "kp") else ("sh" if id_type == "shikimori" else "kp")
    key = serv + id  # раньше здесь всегда был 'sh'+id для имени и 'kp'+id для кеша — оба варианта были ошибкой
    metadata = {}
    cached = None
    if ch_use and ch:
        try:
            cached = ch.get_data_by_id(key)
        except KeyError:
            cached = None
    if cached:
        base = str(cached['title'])
        if seria_num != 0:
            fname = f"{base}-Серия-{str(seria_num).zfill(add_zeros)}-Перевод-{translation}-{quality}p"
        else:
            fname = f"{base}-Перевод-{translation}-{quality}p"
        metadata = {
            'title': f"{cached['title']} - Серия-{seria_num}" if seria_num != 0 else cached['title'],
            'year': cached['year'],
            'date': cached['year'],
            'comment': cached['description'],
            'artist': translation,
            'track': seria_num,
        }
    else:
        fname = f'Перевод-{translation}-{quality}p' if seria_num == 0 else f'Серия-{str(seria_num).zfill(add_zeros)}-Перевод-{translation}-{quality}p'
    if len(fname) > 128:  # Лимит имени файла: 255 байт в линуксе = ~128 символов кириллицы
        if len(translation) > 100:
            fname = f'{quality}p' if seria_num == 0 else f'Серия-{str(seria_num).zfill(add_zeros)}-{quality}p'
        else:
            fname = f'Перевод-{translation}-{quality}p' if seria_num == 0 else f'Серия-{str(seria_num).zfill(add_zeros)}-Перевод-{translation}-{quality}p'
    fname = batch_download._sanitize(fname, 128)
    try:
        hsh, link = fast_download(id, id_type, seria_num, translation_id, quality, config.KODIK_TOKEN,
                                  filename=fname, metadata=metadata)
        if ch_save and ch and link:
            try:
                ch.add_seria(key, translation_id, seria_num, link)
            except KeyError:
                pass
        return send_file(get_path(hsh), as_attachment=True)
    except ModuleNotFoundError:
        return abort(500, 'Внимание, на сервере не установлен ffmpeg или программа не может получить к нему доступ. Ffmpeg обязателен для использования быстрой загрузки. (Стандартная загрузка работает без ffmpeg)')
    except FileNotFoundError:
        return abort(404, 'Видеофайл не найден, попробуйте сменить качество')


@app.route('/fast_download/<string:id_type>-<string:id>-<int:seria_num>-<string:translation_id>-<string:quality>/')
@app.route('/fast_download/<string:id_type>-<string:id>-<int:seria_num>-<string:translation_id>-<string:quality>-<int:max_series>/')
def fast_download_prepare(id_type: str, id: str, seria_num: int, translation_id: str, quality: str, max_series: int = 12):
    return render_template('fast_download_prepare.html', seria_num=seria_num,
                           url=f'/fast_download_act/{id_type}-{id}-{seria_num}-{translation_id}-{quality}-{max_series}/',
                           past_url=request.referrer if request.referrer else f'/download/{id_type}/{id}/',
                           is_dark=is_dark(), is_mobile=g.is_mobile)


# --------------------------------------------------------- Пакетная загрузка
@app.route('/batch/<string:serv>/<string:id>/<string:data>/')
def batch_page(serv, id, data):
    try:
        series, translation_id = parse_data(data)
    except (IndexError, ValueError):
        return abort(400)
    min_series = max(series[0], 1)
    max_series = max(series[1], min_series)
    translation_name = translations.get(translation_id, 'Неизвестно')
    title = cached_title(serv, id) or 'Выбранный тайтл'
    return render_template('batch_download.html',
        serv=serv, id=id, data=data,
        translation_id=translation_id, translation_name=translation_name,
        min_series=min_series, max_series=max_series, title=title,
        backlink=f'/download/{serv}/{id}/{data}/',
        is_dark=is_dark(), is_mobile=g.is_mobile)


@app.route('/batch/start/', methods=['POST'])
def batch_start():
    d = request.get_json(silent=True) or {}
    try:
        job_id = batch_download.start_job(
            serv=d.get('serv'),
            id=str(d.get('id', '')),
            translation_id=str(d.get('translation_id', '')),
            translation_name=d.get('translation_name', 'Неизвестно'),
            quality=str(d.get('quality', '')),
            start=int(d.get('start', 0)),
            end=int(d.get('end', 0)),
            title=d.get('title', ''),
        )
    except (ValueError, TypeError) as ex:
        return {'error': str(ex)}, 400
    except ModuleNotFoundError:
        return {'error': 'На сервере не установлен ffmpeg — он обязателен для пакетной загрузки.'}, 500
    return {'job_id': job_id}


@app.route('/batch/status/<string:job_id>/')
def batch_status(job_id):
    st = batch_download.get_status(job_id)
    if st is None:
        return {'error': 'Задача не найдена (возможно, сервер был перезапущен)'}, 404
    return st


@app.route('/batch/download/<string:job_id>/')
def batch_zip(job_id):
    res = batch_download.get_zip_path(job_id)
    if not res or not os.path.exists(res[0]):
        return abort(404)
    path, name = res
    return send_file(os.path.abspath(path), as_attachment=True, download_name=name)


@app.route('/batch/cancel/<string:job_id>/', methods=['POST'])
def batch_cancel(job_id):
    batch_download.cancel_job(job_id)
    return {'ok': True}


# --------------------------------------------------------------- Сокеты
@socketio.on('join')
def on_join(data):
    join_room(data['rid'])
    if not watch_manager.is_room(data['rid']):
        return
    watch_manager.room_used(data['rid'])
    return send({'data': {'status': 'loading', 'time': watch_manager.get_room_data(data['rid'])['play_time']}}, to=data['rid'])


@socketio.on('broadcast')
def broadcast(data):
    if not watch_manager.is_room(data.get('rid', '')):
        return
    watch_manager.room_used(data['rid'])
    watch_manager.update_play_time(data['rid'], data['data']['time'])
    return send(data, to=data['rid'])


# --------------------------------------------------------------- Прочее
@app.route('/help/')
def help():
    # Заглушка
    return redirect("https://github.com/YaNesyTortiK/Kodik-Download-Watch/blob/main/README.MD")


@app.route('/resources/<path:path>')
def resources(path: str):
    # send_from_directory защищает от path traversal (раньше путь клеился руками)
    return send_from_directory('resources', path)


@app.route('/favicon.ico')
def favicon():
    return send_file(config.FAVICON_PATH)


if __name__ == "__main__":
    socketio.run(app, host=config.HOST, port=config.PORT, debug=config.DEBUG)
