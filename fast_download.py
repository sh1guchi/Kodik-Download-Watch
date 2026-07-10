from hashlib import md5
from concurrent.futures import ThreadPoolExecutor
from getters import get_download_link, get_url_data
import requests
import os
import shutil
import subprocess

# Сколько сегментов качаем одновременно. Раньше создавался поток на каждый
# сегмент (150+ потоков на серию) — из-за этого и появлялись SSLError.
SEGMENT_WORKERS = 16


def fast_download(id: str, id_type: str, seria_num: int, translation_id: str, quality: str, token: str,
                  filename: str = 'result', metadata: dict = {}) -> tuple[str, str]:
    """
    Быстрая загрузка за счёт параллельного скачивания фрагментов HLS.
    :id: Id сериала на Шикимори/Кинопоиске
    :id_type: тип id 'shikimori' или 'kinopoisk' ('sh' или 'kp')
    :seria_num: номер серии
    :translation_id: id перевода/субтитров (Прим: 610 - AniLibria.TV)
    :quality: качество ('360'/'480'/'720')
    :token: токен Kodik

    Возвращает (hsh, link): хэш для get_path и прямую ссылку на серию.
    """
    check_ffmpeg()  # Проверка на доступность ffmpeg из модуля subprocess
    hsh = md5(str(id + id_type + translation_id + str(seria_num) + quality).encode('utf-8')).hexdigest() + "~"
    os.makedirs('tmp', exist_ok=True)
    workdir = os.path.join('tmp', hsh)
    if os.path.exists(workdir):
        if any(x.endswith('.mp4') for x in os.listdir(workdir)):
            # Уже есть собранный результат для этого хэша.
            # ВАЖНО: раньше здесь возвращался только hsh (не кортеж) — краш
            # при распаковке в main.py. Возвращаем кортеж с пустой ссылкой.
            return (hsh, '')
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)

    if id_type == 'sh':
        id_type = 'shikimori'
    elif id_type == 'kp':
        id_type = 'kinopoisk'
    link = get_download_link(id, id_type, seria_num, translation_id, token)
    manifest = get_url_data('https:' + link + quality + '.mp4:hls:manifest.m3u8')
    segments = get_segments(manifest, 'https:' + link)
    if not segments:
        raise FileNotFoundError('Не удалось получить сегменты видео (возможно, нет такого качества)')

    def _pull(seg):
        download_segment(seg[0], os.path.join(workdir, seg[1] + '.ts'))

    with ThreadPoolExecutor(max_workers=SEGMENT_WORKERS) as pool:
        # list() чтобы дождаться всех и увидеть исключения
        list(pool.map(_pull, segments))

    combine_segments(workdir, name=filename.replace(' ', '-'), metadata=metadata)
    return (hsh, link)


def get_segments(manifest: str, original_link: str) -> list:
    res = []
    manifest = manifest.split('\n')[7:]
    for i in range(0, len(manifest), 2):
        if manifest[i].strip() != '':
            res.append([original_link + manifest[i][2:], manifest[i].split('-')[1]])
    return res


def download_segment(link: str, path: str, retries: int = 3):
    last_exc = None
    for _ in range(retries):
        try:
            res = requests.get(link, timeout=60)
            res.raise_for_status()
            with open(path, 'wb') as f:
                f.write(res.content)
            return
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError,
                requests.exceptions.Timeout, requests.exceptions.HTTPError) as ex:
            last_exc = ex
    raise last_exc


def combine_segments(directory: str, segments_count: int = 0, name: str = 'result',
                     metadata: dict = {}, hwaccel: str | None = None):
    """
    Склеивает .ts-сегменты в mp4 через ffmpeg concat + stream copy.

    Изменения против оригинала:
      * hwaccel по умолчанию None — для '-c copy' ускорение не нужно,
        а на машинах без CUDA 'cuda' просто ронял ffmpeg;
      * команда собирается списком аргументов — метаданные с кавычками,
        пробелами и юникодом больше не ломают команду (и нет shell-инъекции);
      * ошибки ffmpeg больше не глотаются молча.
    """
    files = [x for x in os.listdir(directory) if x.endswith('.ts')]
    files.sort(key=lambda x: int(x[:-3]))
    if not files:
        raise FileNotFoundError('Сегменты для сборки не найдены')

    list_path = os.path.join(directory, 'files.txt')
    with open(list_path, 'w', encoding='utf-8') as f:
        for file in files:
            f.write(f"file '{file}'\n")

    out_path = os.path.join(directory, f'{name}.mp4')
    cmd = ['ffmpeg', '-y']
    if hwaccel:
        cmd += ['-hwaccel', str(hwaccel)]
    cmd += ['-f', 'concat', '-safe', '0', '-i', list_path, '-c', 'copy']
    for k, v in (metadata or {}).items():
        cmd += ['-metadata', f'{k}={v}']
    cmd += [out_path]

    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not os.path.exists(out_path):
        tail = proc.stderr.decode('utf-8', 'ignore')[-400:] if proc.stderr else ''
        raise FileNotFoundError(f'ffmpeg не смог собрать видео. {tail}')


def get_path(hsh: str) -> str:
    workdir = os.path.join('tmp', hsh)
    if not os.path.exists(workdir):
        raise FileNotFoundError(f'Temporary directory with hash "{hsh}" not found')
    x = [os.path.join(workdir, f) for f in os.listdir(workdir) if f.endswith('.mp4')]
    if not x:
        raise FileNotFoundError(f'Result .mp4 file not found in "{hsh}" directory')
    return x[0]


def check_ffmpeg():
    """
    Raises ModuleNotFoundError if ffmpeg isn't installed or can't be used by subprocess
    """
    try:
        subprocess.run(['ffmpeg', '-version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        raise ModuleNotFoundError('Ffmpeg is required to use fast download.')


def clear_tmp():
    """
    Clears tmp directory (Creates if not found). Use on the start of application.
    """
    if os.path.exists('tmp'):
        shutil.rmtree('tmp', ignore_errors=True)
    os.makedirs('tmp', exist_ok=True)
