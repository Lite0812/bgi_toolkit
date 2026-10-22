"""BM converter GUI, sharing the fixed codec with bgi_toolkit.bm_codec."""
from ..gui.localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import tr
from ..bm_codec import *
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QFileDialog,
    QMessageBox, QCheckBox, QFrame, QProgressBar, QComboBox, QSpinBox,
    QRadioButton, QButtonGroup, QSlider
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QWaitCondition, QMutex

def _restore_transparent_from_black_rgb(img_rgb: Image.Image) -> Image.Image:
    img_rgb = img_rgb.convert("RGB")
    w, h = img_rgb.size
    if np is None:
        src = list(img_rgb.getdata())
        out = []
        for r, g, b in src:
            a = r if r >= g else g
            a = a if a >= b else b
            if a <= 0:
                out.append((0, 0, 0, 0))
                continue
            alpha_norm = a / 255.0
            rr = int(r / alpha_norm + 0.5)
            gg = int(g / alpha_norm + 0.5)
            bb = int(b / alpha_norm + 0.5)
            if rr < 0: rr = 0
            if gg < 0: gg = 0
            if bb < 0: bb = 0
            if rr > 255: rr = 255
            if gg > 255: gg = 255
            if bb > 255: bb = 255
            out.append((rr, gg, bb, a))
        img_rgba = Image.new("RGBA", (w, h))
        img_rgba.putdata(out)
        return img_rgba

    arr = np.asarray(img_rgb, dtype=np.float32)
    r = arr[:, :, 0]
    g = arr[:, :, 1]
    b = arr[:, :, 2]
    alpha = np.maximum(np.maximum(r, g), b)
    alpha_norm = alpha / 255.0

    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    mask = alpha_norm > 0
    rgba[:, :, 0][mask] = np.clip(r[mask] / alpha_norm[mask], 0, 255).astype(np.uint8)
    rgba[:, :, 1][mask] = np.clip(g[mask] / alpha_norm[mask], 0, 255).astype(np.uint8)
    rgba[:, :, 2][mask] = np.clip(b[mask] / alpha_norm[mask], 0, 255).astype(np.uint8)
    rgba[:, :, 3] = alpha.astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")

def _save_png_bytes(path: str, width: int, height: int, rgba_bytes: bytes):
    img = Image.frombytes("RGBA", (width, height), rgba_bytes)
    img.save(path)
    img.close()

def _fix_png_file(path: str):
    with Image.open(path) as im:
        fixed = _restore_transparent_from_black_rgb(im)
        fixed.save(path)

def _wnnm_denoise_rgb(rgb_u8, strength: int):
    if np is None:
        return rgb_u8
    if rgb_u8 is None:
        return rgb_u8
    if rgb_u8.dtype != np.uint8:
        rgb_u8 = rgb_u8.astype(np.uint8, copy=False)
    if rgb_u8.ndim != 3 or rgb_u8.shape[2] != 3:
        return rgb_u8

    try:
        s = int(strength)
    except Exception:
        s = 10
    if s < 1:
        s = 1
    if s > 50:
        s = 50

    h, w, _ = rgb_u8.shape
    patch = 5
    half = patch // 2
    pix = h * w
    if pix >= 1280 * 720:
        search_r = 5
        sample_step = 3
        step = 8
    elif pix >= 800 * 600:
        search_r = 6
        sample_step = 3
        step = 6
    else:
        search_r = 6
        sample_step = 2
        step = 4
    k = 8

    img = rgb_u8.astype(np.float32) / 255.0
    lum = img[:, :, 0] * 0.299 + img[:, :, 1] * 0.587 + img[:, :, 2] * 0.114
    pad = half + search_r + 1

    lum_p = np.pad(lum, ((pad, pad), (pad, pad)), mode="reflect")

    acc = np.zeros((lum_p.shape[0], lum_p.shape[1]), dtype=np.float32)
    wacc = np.zeros((lum_p.shape[0], lum_p.shape[1]), dtype=np.float32)

    sigma2 = (s / 255.0) ** 2

    if HAS_OPTIMIZATION and numba is not None:
        @jit(nopython=True, cache=True)
        def _wnnm_select_best_k(lum_p2, cy2, cx2, half2, search_r2, sample_step2, k2):
            patch2 = half2 * 2 + 1
            y0 = cy2 - search_r2
            y1 = cy2 + search_r2
            x0 = cx2 - search_r2
            x1 = cx2 + search_r2

            best_d = np.empty(k2, dtype=np.float32)
            best_y = np.empty(k2, dtype=np.int32)
            best_x = np.empty(k2, dtype=np.int32)
            for i in range(k2):
                best_d[i] = 1e30
                best_y[i] = cy2
                best_x[i] = cx2

            for yy in range(y0, y1 + 1, sample_step2):
                for xx in range(x0, x1 + 1, sample_step2):
                    d = 0.0
                    for py in range(patch2):
                        ry = cy2 - half2 + py
                        ty = yy - half2 + py
                        for px in range(patch2):
                            rx = cx2 - half2 + px
                            tx = xx - half2 + px
                            diff = lum_p2[ty, tx] - lum_p2[ry, rx]
                            d += diff * diff

                    max_i = 0
                    max_d = best_d[0]
                    for i in range(1, k2):
                        if best_d[i] > max_d:
                            max_d = best_d[i]
                            max_i = i
                    if d < max_d:
                        best_d[max_i] = d
                        best_y[max_i] = yy
                        best_x[max_i] = xx

            for i in range(k2 - 1):
                for j in range(i + 1, k2):
                    if best_d[j] < best_d[i]:
                        td = best_d[i]
                        best_d[i] = best_d[j]
                        best_d[j] = td
                        ty = best_y[i]
                        best_y[i] = best_y[j]
                        best_y[j] = ty
                        tx = best_x[i]
                        best_x[i] = best_x[j]
                        best_x[j] = tx
            return best_y, best_x

    def _shrink(P):
        U, sv, Vt = np.linalg.svd(P, full_matrices=False)
        wv = (sigma2 / (sv + 1e-8))
        sv2 = sv - wv
        sv2[sv2 < 0] = 0
        return (U * sv2) @ Vt

    for y in range(0, h, step):
        cy = y + pad
        for x in range(0, w, step):
            cx = x + pad
            y0 = cy - search_r
            y1 = cy + search_r
            x0 = cx - search_r
            x1 = cx + search_r

            if HAS_OPTIMIZATION and numba is not None:
                best_y, best_x = _wnnm_select_best_k(lum_p, cy, cx, half, search_r, sample_step, k)
                patches = []
                for idx in range(k):
                    yy = int(best_y[idx])
                    xx = int(best_x[idx])
                    patches.append(lum_p[yy - half : yy + half + 1, xx - half : xx + half + 1])
            else:
                ref = lum_p[cy - half : cy + half + 1, cx - half : cx + half + 1]
                candidates = []
                for yy in range(y0, y1 + 1, sample_step):
                    for xx in range(x0, x1 + 1, sample_step):
                        p = lum_p[yy - half : yy + half + 1, xx - half : xx + half + 1]
                        d = float(np.sum((p - ref) ** 2))
                        candidates.append((d, yy, xx))
                candidates.sort(key=lambda t: t[0])
                best = candidates[:k]
                patches = []
                for _, yy, xx in best:
                    patches.append(lum_p[yy - half : yy + half + 1, xx - half : xx + half + 1])
            group = np.stack(patches, axis=0).reshape((len(patches), patch * patch)).T
            recon = _shrink(group)
            out_patch = recon[:, 0].reshape((patch, patch))

            yy0 = cy - half
            xx0 = cx - half
            acc[yy0 : yy0 + patch, xx0 : xx0 + patch] += out_patch
            wacc[yy0 : yy0 + patch, xx0 : xx0 + patch] += 1.0

    region = (slice(pad, pad + h), slice(pad, pad + w))
    denom = wacc[region[0], region[1]]
    mask = denom > 0
    out_lum = lum.copy()
    if np.any(mask):
        out_lum[mask] = acc[region[0], region[1]][mask] / denom[mask]
    out_lum = np.clip(out_lum, 0.0, 1.0)

    eps = 1e-6
    ratio = out_lum / (lum + eps)
    ratio = np.where(lum > eps, ratio, 1.0)
    ratio = np.clip(ratio, 0.0, 3.0)
    out = np.clip(img * ratio[:, :, None], 0.0, 1.0)
    return (out * 255.0 + 0.5).astype(np.uint8)

def _apply_deblock_rgba_bytes(rgba_bytes: bytes, width: int, height: int, method: str, strength: int) -> bytes:
    if np is None:
        return rgba_bytes
    if method == "ffmpeg":
        return rgba_bytes
    try:
        s = int(strength)
    except Exception:
        s = 10
    if s < 1:
        s = 1
    if s > 50:
        s = 50

    rgba = np.frombuffer(rgba_bytes, dtype=np.uint8)
    if rgba.size != width * height * 4:
        return rgba_bytes
    rgba = rgba.reshape((height, width, 4))

    alpha = rgba[:, :, 3].copy()
    if method == "wnnm":
        rgb = rgba[:, :, :3].copy()
        out_rgb = _wnnm_denoise_rgb(rgb, s)
        out = np.empty((height, width, 4), dtype=np.uint8)
        out[:, :, :3] = out_rgb
        out[:, :, 3] = alpha
        return out.tobytes()
    if cv2 is None:
        return rgba_bytes
    try:
        bgr = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
    except Exception:
        bgr = cv2.cvtColor(rgba.copy(), cv2.COLOR_RGBA2BGR)

    if method == "nlmeans":
        h = s
        out_bgr = cv2.fastNlMeansDenoisingColored(bgr, None, h, h, 7, 21)
    elif method == "median":
        k = 3 + 2 * (s // 10)
        if k > 9:
            k = 9
        out_bgr = cv2.medianBlur(bgr, k)
    elif method == "gaussian":
        k = 3 + 2 * (s // 10)
        if k > 11:
            k = 11
        sigma = max(0.1, s / 10.0)
        out_bgr = cv2.GaussianBlur(bgr, (k, k), sigmaX=sigma, sigmaY=sigma)
    elif method == "edgepreserve":
        fn = getattr(cv2, "edgePreservingFilter", None)
        if fn is None:
            sigma = max(10, s * 10)
            out_bgr = cv2.bilateralFilter(bgr, 7, sigma, sigma)
        else:
            sigma_s = max(10, min(200, s * 10))
            sigma_r = max(0.05, min(1.0, s / 25.0))
            out_bgr = fn(bgr, flags=1, sigma_s=sigma_s, sigma_r=sigma_r)
    elif method == "bm3d":
        xphoto = getattr(cv2, "xphoto", None)
        fn = getattr(xphoto, "bm3dDenoising", None) if xphoto is not None else None
        if fn is None:
            h = s
            out_bgr = cv2.fastNlMeansDenoisingColored(bgr, None, h, h, 7, 21)
        else:
            h = float(s)
            out_bgr = None
            for args in ((bgr, h), (bgr, None, h), (bgr,)):
                try:
                    out_bgr = fn(*args)
                    break
                except Exception:
                    out_bgr = None
            if out_bgr is None or getattr(out_bgr, "shape", None) != bgr.shape:
                h = s
                out_bgr = cv2.fastNlMeansDenoisingColored(bgr, None, h, h, 7, 21)
    else:
        sigma = max(10, s * 10)
        out_bgr = cv2.bilateralFilter(bgr, 7, sigma, sigma)

    rgb = cv2.cvtColor(out_bgr, cv2.COLOR_BGR2RGB)
    out = np.empty((height, width, 4), dtype=np.uint8)
    out[:, :, :3] = rgb
    out[:, :, 3] = alpha
    return out.tobytes()

def _alpha_is_fully_opaque_bytes(rgba_bytes: bytes) -> bool:
    mv = memoryview(rgba_bytes)
    for i in range(3, len(mv), 4):
        if mv[i] != 255:
            return False
    return True

def _alpha_is_fully_opaque_image(img: Image.Image) -> bool:
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    alpha = img.getchannel("A")
    min_a, max_a = alpha.getextrema()
    return min_a == 255 and max_a == 255

def _alpha_is_fully_opaque_from_decoder(decoder, width: int, height: int) -> bool:
    if HAS_OPTIMIZATION:
        arr = decoder.output.reshape((decoder.height, decoder.width, 4))
        alpha = arr[:height, :width, 3]
        return bool(alpha.min() == 255 and alpha.max() == 255)
    out = decoder.output
    total = width * height * 4
    for i in range(3, total, 4):
        if out[i] != 255:
            return False
    return True


# ==========================================
# 工作线程与逻辑
# ==========================================

class Worker(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)
    progress_signal = pyqtSignal(int, int)
    status_signal = pyqtSignal(object)

    def __init__(self, input_path, output_path, export_video, video_format, use_custom_fps, custom_fps, crf_value=20, ffmpeg_path="ffmpeg", remove_black_bg_24bit=False, remove_black_bg_32_opaque=False, enable_deblock=False, deblock_method="bilateral", deblock_strength=10, enable_multithread=False, enable_multiprocess=False, thread_count=1, export_color_mask=False, export_side_mask=False, keep_original_video=False, enable_file_multiprocess=False, file_process_count=1, force_black_bg_files=None):
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.export_video = export_video
        self.video_format = video_format
        self.use_custom_fps = use_custom_fps
        self.custom_fps = custom_fps
        self.crf_value = crf_value
        self.ffmpeg_path = ffmpeg_path
        self.remove_black_bg_24bit = remove_black_bg_24bit
        self.remove_black_bg_32_opaque = remove_black_bg_32_opaque
        self.enable_deblock = bool(enable_deblock)
        self.deblock_method = str(deblock_method or "bilateral")
        try:
            self.deblock_strength = int(deblock_strength)
        except Exception:
            self.deblock_strength = 10
        self.enable_multithread = bool(enable_multithread)
        self.enable_multiprocess = bool(enable_multiprocess)
        self.thread_count = _normalize_parallel_count(thread_count) if (self.enable_multithread or self.enable_multiprocess) else 1
        self.enable_file_multiprocess = bool(enable_file_multiprocess)
        self.file_process_count = _normalize_parallel_count(file_process_count) if self.enable_file_multiprocess else 1
        self.export_color_mask = bool(export_color_mask)
        self.export_side_mask = bool(export_side_mask)
        self.keep_original_video = bool(keep_original_video)
        self.force_black_bg_files = set()
        if force_black_bg_files:
            for p in force_black_bg_files:
                if not p:
                    continue
                self.force_black_bg_files.add(os.path.normcase(os.path.abspath(p)))
        self.total_frames = 0
        self.current_frame = 0
        self._is_paused = False
        self._is_stopped = False
        self._pause_cond = QWaitCondition()
        self._mutex = QMutex()
        self._mp_manager = None
        self._mp_log_queue = None
        self._mp_progress_queue = None
        self._mp_stop_event = None
        self._mp_pause_event = None

        # 检测是否可以使用 C 解码库
        self.use_c_decoder = (_load_decoder_lib() is not None)
        if self.use_c_decoder:
            _set_c_decoder_threads(self.thread_count)

    def set_paused(self, paused):
        self._mutex.lock()
        self._is_paused = paused
        if self._mp_pause_event is not None:
            if paused:
                self._mp_pause_event.set()
            else:
                ui_call(self._mp_pause_event, 'clear')
        if not paused:
            self._pause_cond.wakeAll()
        self._mutex.unlock()

    def stop(self):
        self._mutex.lock()
        self._is_stopped = True
        self._is_paused = False
        if self._mp_stop_event is not None:
            self._mp_stop_event.set()
        if self._mp_pause_event is not None:
            ui_call(self._mp_pause_event, 'clear')
        self._pause_cond.wakeAll()
        self._mutex.unlock()

    def check_pause_stop(self):
        self._mutex.lock()
        if self._is_stopped:
            self._mutex.unlock()
            return True
        
        if self._is_paused:
            self._pause_cond.wait(self._mutex)
        
        if self._is_stopped:
            self._mutex.unlock()
            return True
            
        self._mutex.unlock()
        return False

    def run(self):
        try:
            bm_files = []
            if os.path.isfile(self.input_path):
                bm_files = [self.input_path]
            elif os.path.isdir(self.input_path):
                for root, _, files in os.walk(self.input_path):
                    for file in files:
                        if file.lower().endswith(".bm"):
                            bm_files.append(os.path.join(root, file))
            else:
                self.finished_signal.emit(False, tr('输入路径不存在'))
                return

            if not bm_files:
                self.finished_signal.emit(False, tr('未找到 BM 文件'))
                return

            self.log_signal.emit(tr('检测到 {0} 个 BM 文件', len(bm_files)))
            if self.use_c_decoder:
                self.log_signal.emit(tr('已启用 C 语言加速解码'))
            elif HAS_OPTIMIZATION:
                self.log_signal.emit(tr('已启用 Numpy/Numba 加速'))
            else:
                self.log_signal.emit(tr('未检测到加速库，使用纯 Python 模式 (较慢)'))
            
            if self.enable_multiprocess:
                self.log_signal.emit(tr('已启用多进程: 进程数 {0}', self.thread_count))
            elif self.enable_multithread:
                self.log_signal.emit(tr('已启用多线程: 线程数 {0}', self.thread_count))
            else:
                self.log_signal.emit(tr('多线程未启用'))
            if self.enable_file_multiprocess and len(bm_files) > 1:
                self.log_signal.emit(tr('已启用文件多进程: 进程数 {0}', self.file_process_count))
            
            if self.export_video and self.export_color_mask:
                if self.export_side_mask:
                    self.log_signal.emit(tr("已启用 Ren'Py 侧边遮罩单视频输出"))
                else:
                    self.log_signal.emit(tr("已启用 Ren'Py 色彩/遮罩双视频输出"))
                if self.export_side_mask:
                    self.log_signal.emit(tr("已启用 Ren'Py 侧边遮罩视频输出"))

            # Count frames
            self.status_signal.emit(tr('统计帧数...'))
            file_total_frames = {}
            for f in bm_files:
                base_name = os.path.splitext(os.path.basename(f))[0]
                cnt = get_frame_count(f)
                file_total_frames[base_name] = cnt
                self.total_frames += cnt
            
            self.progress_signal.emit(0, self.total_frames)
            self.status_signal.emit(tr('开始处理...'))

            if self.enable_file_multiprocess and len(bm_files) > 1:
                ctx = mp.get_context("spawn")
                self._mp_manager = ctx.Manager()
                self._mp_log_queue = self._mp_manager.Queue()
                self._mp_progress_queue = self._mp_manager.Queue()
                self._mp_stop_event = self._mp_manager.Event()
                self._mp_pause_event = self._mp_manager.Event()

                def _log_consumer():
                    while True:
                        msg = self._mp_log_queue.get()
                        if msg is None:
                            return
                        self.log_signal.emit(msg)

                def _progress_consumer():
                    file_current_frames = {}
                    file_last_status = {}

                    def _select_fastest_file():
                        active_files = []
                        completed_files = []
                        for name, cur in file_current_frames.items():
                            total = file_total_frames.get(name, 0)
                            if total <= 0: continue
                            ratio = float(cur) / float(total)
                            if ratio < 1.0 - 1e-6:
                                active_files.append((name, ratio))
                            else:
                                completed_files.append((name, ratio))
                        
                        if active_files:
                            active_files.sort(key=lambda x: x[1], reverse=True)
                            return active_files[0][0]
                        
                        if completed_files:
                            completed_files.sort(key=lambda x: x[1], reverse=True)
                            return completed_files[0][0]
                            
                        return None

                    while True:
                        payload = self._mp_progress_queue.get()
                        if payload is None:
                            return
                        if isinstance(payload, str):
                            self.status_signal.emit(payload)
                            continue
                        if isinstance(payload, tuple) and len(payload) == 3 and isinstance(payload[0], str):
                            tag, name, value = payload
                            if tag == "p":
                                try:
                                    delta_current = int(value)
                                except Exception:
                                    delta_current = 0
                                if delta_current:
                                    file_current_frames[name] = int(file_current_frames.get(name, 0)) + delta_current
                                    self.current_frame += delta_current
                                    self.progress_signal.emit(self.current_frame, self.total_frames)
                                fastest = _select_fastest_file()
                                if fastest is not None:
                                    msg = file_last_status.get(fastest) or tr('正在处理: {0}', fastest)
                                    self.status_signal.emit(msg)
                                continue
                            if tag == "t":
                                try:
                                    delta_total = int(value)
                                except Exception:
                                    delta_total = 0
                                if delta_total:
                                    self.total_frames += delta_total
                                continue
                            if tag == "s":
                                file_last_status[name] = str(value)
                                fastest = _select_fastest_file()
                                if fastest is not None:
                                    msg = file_last_status.get(fastest) or tr('正在处理: {0}', fastest)
                                    self.status_signal.emit(msg)
                                else:
                                    self.status_signal.emit(str(value))
                                continue
                        if isinstance(payload, tuple) and len(payload) == 2:
                            delta_current, delta_total = payload
                        else:
                            delta_current, delta_total = payload, 0
                        if delta_total:
                            self.total_frames += int(delta_total)
                        self.current_frame += int(delta_current)
                        self.progress_signal.emit(self.current_frame, self.total_frames)

                log_thread = threading.Thread(target=_log_consumer, daemon=True)
                progress_thread = threading.Thread(target=_progress_consumer, daemon=True)
                log_thread.start()
                progress_thread.start()

                config_enable_multithread = self.enable_multithread or self.enable_multiprocess
                
                # 在文件并行模式下，自动降低每个子进程内部的线程数，避免资源过度竞争
                sub_thread_count = self.thread_count
                if self.enable_file_multiprocess and self.file_process_count > 1:
                    # 尝试将总线程数分配给各个进程
                    sub_thread_count = max(1, self.thread_count // self.file_process_count)
                    # 进一步限制上限，避免单进程线程过多
                    sub_thread_count = min(sub_thread_count, 4)

                config = {
                    "output_path": self.output_path,
                    "export_video": self.export_video,
                    "video_format": self.video_format,
                    "use_custom_fps": self.use_custom_fps,
                    "custom_fps": self.custom_fps,
                    "crf_value": self.crf_value,
                    "ffmpeg_path": self.ffmpeg_path,
                    "remove_black_bg_24bit": self.remove_black_bg_24bit,
                    "remove_black_bg_32_opaque": self.remove_black_bg_32_opaque,
                    "enable_deblock": self.enable_deblock,
                    "deblock_method": self.deblock_method,
                    "deblock_strength": self.deblock_strength,
                    "force_black_bg_files": list(self.force_black_bg_files),
                    "enable_multithread": config_enable_multithread,
                    "enable_multiprocess": False,
                    "thread_count": sub_thread_count,
                    "export_color_mask": self.export_color_mask,
                    "export_side_mask": self.export_side_mask,
                    "keep_original_video": self.keep_original_video,
                    "use_c_decoder": self.use_c_decoder
                }

                errors = []
                file_executor = None
                stopped_early = False
                try:
                    file_executor = ProcessPoolExecutor(max_workers=self.file_process_count, mp_context=ctx)
                    futures = [
                        file_executor.submit(
                            _process_file_in_subprocess,
                            bm_file,
                            config,
                            self._mp_log_queue,
                            self._mp_progress_queue,
                            self._mp_stop_event,
                            self._mp_pause_event,
                            self.total_frames
                        )
                        for bm_file in bm_files
                    ]
                    pending_futures = set(futures)
                    while pending_futures:
                        if self.check_pause_stop():
                            stopped_early = True
                            self._mp_stop_event.set()
                            break
                        done, not_done = wait(pending_futures, return_when=FIRST_COMPLETED, timeout=0.2)
                        if not done:
                            continue
                        for fut in done:
                            try:
                                fut.result()
                            except Exception as e:
                                stopped_early = True
                                errors.append(str(e))
                                self._mp_stop_event.set()
                                break
                        pending_futures = not_done
                        if stopped_early:
                            break

                    if stopped_early:
                        try:
                            for f in list(pending_futures):
                                try:
                                    f.cancel()
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        try:
                            procs = getattr(file_executor, "_processes", None)
                            if isinstance(procs, dict):
                                for p in procs.values():
                                    try:
                                        p.terminate()
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                finally:
                    if file_executor is not None:
                        try:
                            if stopped_early:
                                file_executor.shutdown(wait=False, cancel_futures=True)
                            else:
                                file_executor.shutdown(wait=True, cancel_futures=True)
                        except TypeError:
                            file_executor.shutdown(wait=(not stopped_early))
                    if self._mp_log_queue is not None:
                        self._mp_log_queue.put(None)
                    if self._mp_progress_queue is not None:
                        self._mp_progress_queue.put(None)
                    try:
                        log_thread.join(timeout=2)
                        progress_thread.join(timeout=2)
                    except Exception:
                        pass
                    if self._mp_manager is not None:
                        try:
                            self._mp_manager.shutdown()
                        except Exception:
                            pass
                    self._mp_manager = None
                    self._mp_log_queue = None
                    self._mp_progress_queue = None
                    self._mp_stop_event = None
                    self._mp_pause_event = None

                if errors:
                    self.finished_signal.emit(False, errors[0])
                    return
                if self.check_pause_stop():
                    self.finished_signal.emit(False, tr('用户停止任务'))
                    return
            else:
                shared_executor = None
                if not self.export_video:
                    if self.enable_multiprocess:
                        shared_executor = ProcessPoolExecutor(max_workers=self.thread_count)
                    elif self.enable_multithread:
                        shared_executor = ThreadPoolExecutor(max_workers=self.thread_count)

                try:
                    for bm_file in bm_files:
                        if self.check_pause_stop():
                            self.finished_signal.emit(False, tr('用户停止任务'))
                            return
                        self.process_file(bm_file, shared_executor)
                finally:
                    if shared_executor is not None:
                        try:
                            shared_executor.shutdown(wait=True, cancel_futures=True)
                        except TypeError:
                            shared_executor.shutdown(wait=True)

            self.finished_signal.emit(True, tr('所有任务已完成'))
        
        except Exception as e:
            import traceback
            self.log_signal.emit(traceback.format_exc())
            self.finished_signal.emit(False, str(e))

    def process_file(self, filepath, shared_executor=None, force_internal_deblock=False):
        base_name = os.path.splitext(os.path.basename(filepath))[0]
        
        if not self.export_video:
            out_dir = os.path.join(self.output_path, base_name)
            os.makedirs(out_dir, exist_ok=True)
        else:
            out_dir = self.output_path
            os.makedirs(out_dir, exist_ok=True)
        
        self.log_signal.emit(tr('正在处理: {0}', os.path.basename(filepath)))
        try:
            parts = [tr('正在处理')]
            if self.enable_deblock:
                parts.append(tr('去块'))
            if self.remove_black_bg_24bit or self.remove_black_bg_32_opaque or (self.force_black_bg_files and os.path.normcase(os.path.abspath(filepath)) in self.force_black_bg_files):
                parts.append(tr('去黑底'))
            if self.export_video and self.export_color_mask:
                if self.export_side_mask:
                    parts.append(tr('侧边遮罩'))
                else:
                    parts.append(tr('色彩/遮罩'))
            self.status_signal.emit(f"{'+'.join(parts)}: {base_name}")
        except Exception:
            pass
        
        with open(filepath, 'rb', buffering=1024 * 1024) as f:
            magic = f.read(16)
            if b'BF_Movie' not in magic:
                self.log_signal.emit(tr('  非 BF_Movie 文件，跳过'))
                return
            
            f.seek(0x14)
            width = struct.unpack('<I', f.read(4))[0]
            f.seek(0x18)
            height = struct.unpack('<I', f.read(4))[0]

            f.seek(0x1C)
            header_png_bit_depth = struct.unpack('<I', f.read(4))[0]
            
            f.seek(0x24)
            file_fps = struct.unpack('<I', f.read(4))[0]
            
            f.seek(0x40)
            quant_matrices = f.read(128)  # 128 字节
            
            frame_offsets = read_frame_offsets(f)
            frame_count = len(frame_offsets)
            f.seek(0, 2)
            file_size = f.tell()
            
            fps = self.custom_fps if self.use_custom_fps else (file_fps if file_fps > 0 else 30)
            header_bd_str = tr('{0}位', header_png_bit_depth) if header_png_bit_depth in (24, 32) else f"unknown({header_png_bit_depth})"
            self.log_signal.emit(tr('  分辨率: {0}x{1}, 位深度: {2}, 帧数: {3}, 帧率: {4} ', width, height, header_bd_str, frame_count, fps))

            # 根据是否使用 C 解码器来决定创建解码器对象（仅用于 Python 回退）
            decoder = None
            if not self.use_c_decoder:
                decoder = ParallelCbgDecoder(width, height, 32, quant_matrices)

            remove_black_bg_24 = self.remove_black_bg_24bit
            use_black_bg_24 = remove_black_bg_24 and header_png_bit_depth == 24
            use_black_bg_32 = self.remove_black_bg_32_opaque and header_png_bit_depth == 32

            deblock_enabled_cfg = bool(getattr(self, "enable_deblock", False))
            deblock_method_cfg = str(getattr(self, "deblock_method", "ffmpeg") or "ffmpeg")
            deblock_strength_cfg = getattr(self, "deblock_strength", 10)
            use_ffmpeg_deblock = deblock_enabled_cfg and deblock_method_cfg == "ffmpeg"
            if force_internal_deblock:
                use_ffmpeg_deblock = False
                if deblock_method_cfg == "ffmpeg":
                    deblock_method_cfg = "bilateral"
            if use_ffmpeg_deblock and self.export_video and 0 < frame_count < 6:
                self.log_signal.emit(tr('  帧数过少，FFmpeg 去块已自动切换为双边滤波去块'))
                use_ffmpeg_deblock = False
                if deblock_method_cfg == "ffmpeg":
                    deblock_method_cfg = "bilateral"

            def _build_ffmpeg_deblock_vf(mode: str) -> str:
                try:
                    s = int(deblock_strength_cfg)
                except Exception:
                    s = 10
                if s < 1:
                    s = 1
                if s > 50:
                    s = 50
                ls = min(10.0, max(0.0, s / 6.0))
                cs = min(10.0, max(0.0, s / 8.0))
                lt = 0.0
                ct = 0.0
                core = f"hqdn3d={ls:.2f}:{cs:.2f}:{lt:.2f}:{ct:.2f}"
                if mode == "rgba":
                    return f"format=rgba,split=2[a][b];[b]alphaextract[alpha];[a]format=yuv444p,{core},format=rgb24[clean];[clean][alpha]alphamerge,format=rgba"
                return f"format=yuv444p,{core},format=rgb24"

            ffmpeg_processes = {}
            ffmpeg_deblock_proc = None
            output_files = {}
            png_pattern = None

            executor = shared_executor
            pending = []
            max_pending = max(2, self.thread_count * 2)

            writer_queues = {}
            writer_threads = {}
            writer_excs = {}
            ffmpeg_stderr_tails = {}
            ffmpeg_stderr_threads = {}

            def _drain_pending(return_when):
                nonlocal pending
                if not pending:
                    return
                done, not_done = wait(pending, return_when=return_when)
                for fut in done:
                    fut.result()
                pending = list(not_done)

            def _save_png(img_to_save: Image.Image, save_path: str):
                img_to_save.save(save_path)
                img_to_save.close()

            def _fix_png(p: str):
                with Image.open(p) as im:
                    fixed = _restore_transparent_from_black_rgb(im)
                    fixed.save(p)

            def _start_writer_for(process: subprocess.Popen):
                # 增加队列深度，防止高并发时阻塞主线程，提高吞吐量
                # 在多文件并行时，如果队列过大可能会占用较多内存，需权衡
                queue_size = max(16, self.thread_count * 8)
                if getattr(self, "enable_file_multiprocess", False):
                    queue_size = max(8, self.thread_count * 4)
                
                q = queue.Queue(maxsize=queue_size)
                exc = {"exc": None}

                def _writer_loop():
                    try:
                        while True:
                            item = q.get()
                            if item is None:
                                return
                            process.stdin.write(item)
                    except Exception as e:
                        exc["exc"] = e

                t = threading.Thread(target=_writer_loop, daemon=True)
                t.start()
                return q, t, exc

            def _append_tail(buf: bytearray, chunk: bytes, limit: int = 16384):
                if not chunk:
                    return
                buf.extend(chunk)
                if len(buf) > limit:
                    del buf[: len(buf) - limit]

            def _start_stderr_drain(key: str, process: subprocess.Popen):
                tail = bytearray()
                ffmpeg_stderr_tails[key] = tail

                def _drain():
                    try:
                        if process is None or process.stderr is None:
                            return
                        while True:
                            data = process.stderr.read(4096)
                            if not data:
                                return
                            _append_tail(tail, data)
                    except Exception:
                        return

                t = threading.Thread(target=_drain, daemon=True)
                ffmpeg_stderr_threads[key] = t
                t.start()

            def _write_frame_bytes(key: str, frame_bytes: bytes):
                q = writer_queues.get(key)
                if q is None:
                    ffmpeg_processes[key].stdin.write(frame_bytes)
                    return

                while True:
                    try:
                        q.put(frame_bytes, timeout=0.1)
                        break
                    except queue.Full:
                        if self.check_pause_stop():
                            return False

                exc = writer_excs.get(key)
                if exc and exc["exc"] is not None:
                    raise exc["exc"]
                return True

            def _scan_all_alpha_opaque():
                scan_use_c = self.use_c_decoder
                scan_decoder = None
                scan_prev = bytes(width * height * 4) if scan_use_c else None
                for i, offset, _, frame_data in iter_frame_bytes(f, frame_offsets, file_size):
                    if self.check_pause_stop():
                        return None

                    if scan_use_c:
                        try:
                            img_bytes = decode_frame_c(frame_data, width, height, 32, quant_matrices, scan_prev)
                            scan_prev = img_bytes
                            if not _alpha_is_fully_opaque_bytes(img_bytes):
                                return False
                            continue
                        except Exception as e:
                            self.log_signal.emit(tr('  C 解码失败，尝试 Python 回退: {0}', e))
                            scan_use_c = False
                            scan_decoder = ParallelCbgDecoder(width, height, 32, quant_matrices)
                            if scan_prev is not None:
                                bgra_prev = _swap_rb_bytes(scan_prev)
                                if HAS_OPTIMIZATION:
                                    scan_decoder.output[:] = np.frombuffer(bgra_prev, dtype=np.uint8)
                                else:
                                    scan_decoder.output[:] = bgra_prev
                    if not scan_use_c:
                        if scan_decoder is None:
                            scan_decoder = ParallelCbgDecoder(width, height, 32, quant_matrices)
                        decode_frame(scan_decoder, frame_data, width, height)
                        if not _alpha_is_fully_opaque_from_decoder(scan_decoder, width, height):
                            return False
                return True

            force_black_bg = False
            if getattr(self, "force_black_bg_files", None):
                force_black_bg = os.path.normcase(os.path.abspath(filepath)) in self.force_black_bg_files

            apply_black_bg_32 = False
            if self.export_video:
                if force_black_bg:
                    self.log_signal.emit(tr('  自定义去黑底文件: 视频分支强制去黑底'))
                    apply_black_bg_32 = True
                elif use_black_bg_32:
                    self.log_signal.emit(tr('  正在检测32bpp是否全不透明...'))
                    apply_black_bg_32 = _scan_all_alpha_opaque()
                    if apply_black_bg_32 is None:
                        return
                    if apply_black_bg_32:
                        self.log_signal.emit(tr('  32bpp 全不透明，启用去黑底'))
                    else:
                        self.log_signal.emit(tr('  32bpp 含透明像素，去黑底跳过'))

            need_black_restore_static = bool(force_black_bg or use_black_bg_24 or apply_black_bg_32)

            retry_with_internal = False
            retry_received = 0
            skip_ffmpeg_log = False
            try:
                startupinfo = None
                if os.name == 'nt':
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                if self.export_video:
                    def _get_output_ext(video_format: str) -> str:
                        if video_format == 'webm':
                            return '.webm'
                        if video_format == 'mov':
                            return '.mov'
                        if video_format == 'apng':
                            return '.apng'
                        if video_format == 'mp4':
                            return '.mp4'
                        if video_format == 'mpg':
                            return '.mpg'
                        return ''

                    def _build_codec_args_main(video_format: str):
                        if video_format == 'webm':
                            crf = str(self.crf_value)
                            return ['-c:v', 'libvpx-vp9', '-b:v', '0', '-crf', crf, '-auto-alt-ref', '0', '-pix_fmt', 'yuva420p']
                        if video_format == 'mov':
                            return ['-c:v', 'prores_ks', '-profile:v', '4444', '-pix_fmt', 'yuva444p10le']
                        if video_format == 'apng':
                            return ['-plays', '0', '-pix_fmt', 'rgba']
                        if video_format == 'mp4':
                            crf = str(self.crf_value)
                            return ['-c:v', 'libx264', '-crf', crf, '-pix_fmt', 'yuv420p']
                        if video_format == 'mpg':
                            q_val = max(1, int(self.crf_value / 2))
                            return ['-c:v', 'mpeg1video', '-q:v', str(q_val), '-pix_fmt', 'yuv420p']
                        return None

                    def _build_codec_args_color(video_format: str):
                        if video_format == 'webm':
                            crf = str(self.crf_value)
                            return ['-c:v', 'libvpx-vp9', '-b:v', '0', '-crf', crf, '-pix_fmt', 'yuv420p']
                        if video_format == 'mov':
                            return ['-c:v', 'prores_ks', '-profile:v', '4444', '-pix_fmt', 'yuv444p10le']
                        if video_format == 'apng':
                            return ['-plays', '0', '-pix_fmt', 'rgb24']
                        if video_format == 'mp4':
                            crf = str(self.crf_value)
                            return ['-c:v', 'libx264', '-crf', crf, '-pix_fmt', 'yuv420p']
                        if video_format == 'mpg':
                            q_val = max(1, int(self.crf_value / 2))
                            return ['-c:v', 'mpeg1video', '-q:v', str(q_val), '-pix_fmt', 'yuv420p']
                        return None

                    def _build_codec_args_mask(video_format: str):
                        if video_format == 'webm':
                            crf = str(self.crf_value)
                            return ['-vf', 'alphaextract', '-c:v', 'libvpx-vp9', '-b:v', '0', '-crf', crf, '-pix_fmt', 'yuv420p']
                        if video_format == 'mov':
                            return ['-vf', 'alphaextract', '-c:v', 'prores_ks', '-profile:v', '0', '-pix_fmt', 'yuv422p10le']
                        if video_format == 'apng':
                            return ['-vf', 'alphaextract', '-plays', '0', '-pix_fmt', 'gray']
                        if video_format == 'mp4':
                            crf = str(self.crf_value)
                            return ['-vf', 'alphaextract', '-c:v', 'libx264', '-crf', crf, '-pix_fmt', 'yuv420p']
                        if video_format == 'mpg':
                            q_val = max(1, int(self.crf_value / 2))
                            return ['-vf', 'alphaextract', '-c:v', 'mpeg1video', '-q:v', str(q_val), '-pix_fmt', 'yuv420p']
                        return None

                    def _build_codec_args_side(video_format: str):
                        side_vf = "format=rgba,split=2[a][b];[b]alphaextract,format=gray,format=rgb24[m];[a]format=rgb24[c];[c][m]hstack"
                        vf_args = ['-vf', side_vf]
                        if video_format == 'webm':
                            crf = str(self.crf_value)
                            return vf_args + ['-c:v', 'libvpx-vp9', '-b:v', '0', '-crf', crf, '-pix_fmt', 'yuv420p']
                        if video_format == 'mov':
                            return vf_args + ['-c:v', 'prores_ks', '-profile:v', '4444', '-pix_fmt', 'yuv444p10le']
                        if video_format == 'apng':
                            return vf_args + ['-plays', '0', '-pix_fmt', 'rgb24']
                        if video_format == 'mp4':
                            crf = str(self.crf_value)
                            return vf_args + ['-c:v', 'libx264', '-crf', crf, '-pix_fmt', 'yuv420p']
                        if video_format == 'mpg':
                            q_val = max(1, int(self.crf_value / 2))
                            return vf_args + ['-c:v', 'mpeg1video', '-q:v', str(q_val), '-pix_fmt', 'yuv420p']
                        return None

                    def _spawn_ffmpeg(key: str, codec_args: list, out_path: str):
                        base_input_args = [
                            self.ffmpeg_path, '-y',
                            '-hide_banner', '-loglevel', 'error', '-nostats',
                            '-f', 'rawvideo',
                            '-vcodec', 'rawvideo',
                            '-s', f'{width}x{height}',
                            '-pix_fmt', 'rgba',
                            '-r', str(fps),
                            '-i', '-',
                        ]
                        # Explicitly set output frame rate to match input frame rate
                        # This prevents ffmpeg from dropping frames or changing frame rate unexpectedly
                        cmd_args = base_input_args + ['-r', str(fps)] + codec_args + [out_path]
                        ffmpeg_processes[key] = subprocess.Popen(
                            cmd_args,
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            startupinfo=startupinfo
                        )
                        _start_stderr_drain(key, ffmpeg_processes[key])
                        if self.enable_multithread or self.enable_multiprocess:
                            q, t, exc = _start_writer_for(ffmpeg_processes[key])
                            writer_queues[key] = q
                            writer_threads[key] = t
                            writer_excs[key] = exc

                    use_color_mask = bool(self.export_color_mask)
                    use_side_mask = bool(getattr(self, "export_side_mask", False))
                    ext = _get_output_ext(self.video_format)
                    if not ext:
                        self.log_signal.emit(tr('  未知输出格式'))
                        return

                    try:
                        if use_color_mask:
                            if use_side_mask:
                                self.log_signal.emit(tr("  正在生成 Ren'Py 侧边遮罩单视频 ({0})...", self.video_format))
                                side_args = _build_codec_args_side(self.video_format)
                                if side_args is None:
                                    self.log_signal.emit(tr('  侧边遮罩失败: 当前格式不支持'))
                                    return
                                output_files["side"] = os.path.join(out_dir, f"{base_name}_side_mask{ext}")
                                _spawn_ffmpeg("side", side_args, output_files["side"])
                            else:
                                self.log_signal.emit(tr("  正在生成 Ren'Py 色彩/遮罩双视频 ({0})...", self.video_format))
                                color_args = _build_codec_args_color(self.video_format)
                                mask_args = _build_codec_args_mask(self.video_format)
                                if color_args is None or mask_args is None:
                                    self.log_signal.emit(tr('  色彩/遮罩失败: 当前格式不支持'))
                                    return
                                output_files["color"] = os.path.join(out_dir, f"{base_name}_color{ext}")
                                output_files["mask"] = os.path.join(out_dir, f"{base_name}_mask{ext}")
                                _spawn_ffmpeg("color", color_args, output_files["color"])
                                _spawn_ffmpeg("mask", mask_args, output_files["mask"])

                            if self.keep_original_video:
                                main_args = _build_codec_args_main(self.video_format)
                                if main_args is None:
                                    self.log_signal.emit(tr('  原始视频跳过: 当前格式不支持'))
                                else:
                                    output_files["main"] = os.path.join(out_dir, f"{base_name}{ext}")
                                    _spawn_ffmpeg("main", main_args, output_files["main"])
                        else:
                            self.log_signal.emit(tr('  正在流式生成视频 ({0})...', self.video_format))
                            main_args = _build_codec_args_main(self.video_format)
                            if main_args is None:
                                self.log_signal.emit(tr('  未知格式'))
                                return
                            output_files["main"] = os.path.join(out_dir, f"{base_name}{ext}")
                            _spawn_ffmpeg("main", main_args, output_files["main"])
                    except FileNotFoundError:
                        self.log_signal.emit(tr('  错误: 未找到 ffmpeg，无法生成视频。'))
                        return
                else:
                    png_pattern = os.path.join(out_dir, f"{base_name}_frame_%04d.png")

                first_png_mode = None
                prev_bytes = None
                if self.use_c_decoder:
                    prev_bytes = bytes(width * height * 4)

                all_alpha_opaque = True if use_black_bg_32 else False
                deblock_enabled = bool(deblock_enabled_cfg)
                deblock_method = str(deblock_method_cfg or "bilateral")
                deblock_strength = deblock_strength_cfg
                deblock_enabled_runtime = deblock_enabled and (not use_ffmpeg_deblock)
                if deblock_enabled_runtime and (cv2 is None or np is None):
                    self.log_signal.emit(tr('  OpenCV 不可用，去块效应已自动关闭'))
                    deblock_enabled_runtime = False
                if deblock_enabled_runtime and deblock_method == "bm3d":
                    xphoto = getattr(cv2, "xphoto", None) if cv2 is not None else None
                    fn = getattr(xphoto, "bm3dDenoising", None) if xphoto is not None else None
                    if fn is None:
                        self.log_signal.emit(tr('  BM3D 不可用(需要 opencv-contrib)，已回退到 非局部均值'))
                        deblock_method = "nlmeans"
                if deblock_enabled_runtime and deblock_method == "edgepreserve":
                    fn = getattr(cv2, "edgePreservingFilter", None) if cv2 is not None else None
                    if fn is None:
                        self.log_signal.emit(tr('  边缘保持 不可用，已回退到 双边滤波'))
                        deblock_method = "bilateral"

                ffmpeg_deblock_mode = "rgb" if need_black_restore_static else "rgba"
                frame_size = width * height * (3 if ffmpeg_deblock_mode == "rgb" else 4)

                if use_ffmpeg_deblock:
                    vf = _build_ffmpeg_deblock_vf(ffmpeg_deblock_mode)
                    cmd_args = [
                        self.ffmpeg_path, '-v', 'error',
                        '-f', 'rawvideo',
                        '-vcodec', 'rawvideo',
                        '-s', f'{width}x{height}',
                        '-pix_fmt', 'rgba',
                        '-r', str(fps),
                        '-i', '-',
                        '-vf', vf,
                        '-f', 'rawvideo',
                        '-pix_fmt', ('rgb24' if ffmpeg_deblock_mode == 'rgb' else 'rgba'),
                        '-'
                    ]
                    try:
                        ffmpeg_deblock_proc = subprocess.Popen(
                            cmd_args,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            startupinfo=startupinfo
                        )
                    except FileNotFoundError:
                        self.log_signal.emit(tr('  错误: 未找到 ffmpeg，无法进行 FFmpeg 去块。'))
                        return
                    ffmpeg_deblock_err_tail = bytearray()

                    def _ffmpeg_deblock_err_drain():
                        try:
                            if ffmpeg_deblock_proc is None or ffmpeg_deblock_proc.stderr is None:
                                return
                            while True:
                                data = ffmpeg_deblock_proc.stderr.read(4096)
                                if not data:
                                    return
                                _append_tail(ffmpeg_deblock_err_tail, data)
                        except Exception:
                            return

                    ffmpeg_deblock_queue = queue.Queue(maxsize=max(4, min(16, self.thread_count * 2)))
                    ffmpeg_deblock_in_queue = queue.Queue(maxsize=max(2, min(8, self.thread_count)))
                    ffmpeg_deblock_exc = {"exc": None}
                    ffmpeg_deblock_sent = [0]
                    ffmpeg_deblock_received = [0]
                    ffmpeg_deblock_lock = threading.Lock()

                    def _ffmpeg_deblock_writer():
                        try:
                            if ffmpeg_deblock_proc is None or ffmpeg_deblock_proc.stdin is None:
                                return
                            while True:
                                item = ffmpeg_deblock_in_queue.get()
                                if item is None:
                                    break
                                ffmpeg_deblock_proc.stdin.write(item)
                        except Exception as e:
                            ffmpeg_deblock_exc["exc"] = e
                        finally:
                            pass

                    def _ffmpeg_deblock_reader():
                        try:
                            if ffmpeg_deblock_proc is None or ffmpeg_deblock_proc.stdout is None:
                                return
                            while True:
                                buf = bytearray()
                                while len(buf) < frame_size:
                                    chunk = ffmpeg_deblock_proc.stdout.read(frame_size - len(buf))
                                    if not chunk:
                                        return
                                    buf.extend(chunk)
                                while True:
                                    try:
                                        ffmpeg_deblock_queue.put(bytes(buf), timeout=0.1)
                                        with ffmpeg_deblock_lock:
                                            ffmpeg_deblock_received[0] += 1
                                        break
                                    except queue.Full:
                                        if self.check_pause_stop():
                                            return
                        except Exception as e:
                            ffmpeg_deblock_exc["exc"] = e
                        finally:
                            try:
                                if ffmpeg_deblock_queue:
                                    ffmpeg_deblock_queue.put(None)
                            except Exception:
                                pass

                    ffmpeg_deblock_writer_thread = threading.Thread(target=_ffmpeg_deblock_writer, daemon=True)
                    ffmpeg_deblock_writer_thread.start()
                    ffmpeg_deblock_err_thread = threading.Thread(target=_ffmpeg_deblock_err_drain, daemon=True)
                    ffmpeg_deblock_err_thread.start()
                    ffmpeg_deblock_thread = threading.Thread(target=_ffmpeg_deblock_reader, daemon=True)
                    ffmpeg_deblock_thread.start()

                def _ffmpeg_deblock_bytes(raw_bytes: bytes) -> bytes:
                    if not use_ffmpeg_deblock or ffmpeg_deblock_proc is None:
                        return raw_bytes
                    if ffmpeg_deblock_proc.stdin is None or ffmpeg_deblock_proc.stdout is None:
                        return raw_bytes
                    exc = ffmpeg_deblock_exc.get("exc") if "ffmpeg_deblock_exc" in locals() else None
                    if exc is not None:
                        raise exc
                    while True:
                        try:
                            ffmpeg_deblock_in_queue.put(raw_bytes, timeout=0.1)
                            with ffmpeg_deblock_lock:
                                ffmpeg_deblock_sent[0] += 1
                            break
                        except queue.Full:
                            if self.check_pause_stop():
                                raise RuntimeError(tr('用户停止任务'))
                            exc = ffmpeg_deblock_exc.get("exc") if "ffmpeg_deblock_exc" in locals() else None
                            if exc is not None:
                                raise exc
                    while True:
                        try:
                            return ffmpeg_deblock_queue.get(timeout=0.1)
                        except queue.Empty:
                            if self.check_pause_stop():
                                raise RuntimeError(tr('用户停止任务'))
                            try:
                                if ffmpeg_deblock_proc is not None and ffmpeg_deblock_proc.poll() is not None:
                                    err = bytes(ffmpeg_deblock_err_tail) if "ffmpeg_deblock_err_tail" in locals() else b""
                                    raise RuntimeError(tr('FFmpeg 去块已退出: {0}', err.decode('utf-8', errors='ignore')))
                            except RuntimeError:
                                raise
                            except Exception:
                                pass
                            exc = ffmpeg_deblock_exc.get("exc") if "ffmpeg_deblock_exc" in locals() else None
                            if exc is not None:
                                raise exc

                use_decoupled_pipeline = bool(self.export_video and use_ffmpeg_deblock)

                if use_decoupled_pipeline:
                    # ==========================================
                    # 解耦流水线模式 (Feeder -> Deblock -> Consumer)
                    # 解决 FFmpeg 滤镜缓冲导致的死锁问题
                    # ==========================================
                    
                    def _feeder_func():
                        nonlocal prev_bytes
                        local_prev_bytes = prev_bytes
                        local_decoder = decoder
                        
                        try:
                            for _, _, _, frame_data in iter_frame_bytes(f, frame_offsets, file_size):
                                if self.check_pause_stop():
                                    break
                                
                                # 解码帧
                                img_bytes = None
                                img = None
                                if self.use_c_decoder:
                                    try:
                                        img_bytes = decode_frame_c(frame_data, width, height, 32, quant_matrices, local_prev_bytes)
                                        local_prev_bytes = img_bytes
                                        if need_black_restore_static:
                                            # 如果需要后续去黑底，这里不需要转 Image，直接传 bytes
                                            pass
                                        else:
                                            # 不需要去黑底，也不需要转 Image，因为是视频导出
                                            pass
                                    except Exception as e:
                                        # 回退到 Python
                                        if local_decoder is None:
                                            local_decoder = ParallelCbgDecoder(width, height, 32, quant_matrices)
                                        decode_frame(local_decoder, frame_data, width, height)
                                        img = build_image(local_decoder, width, height)
                                        local_prev_bytes = img.tobytes()
                                else:
                                    decode_frame(local_decoder, frame_data, width, height)
                                    img = build_image(local_decoder, width, height)

                                # 准备数据发送给 Deblock
                                src_bytes = img_bytes if img_bytes is not None else img.tobytes()
                                if img is not None:
                                    try:
                                        img.close()
                                    except Exception:
                                        pass
                                
                                ffmpeg_deblock_in_queue.put(src_bytes)
                                with ffmpeg_deblock_lock:
                                    ffmpeg_deblock_sent[0] += 1
                                
                        except Exception:
                            pass
                        finally:
                            # 发送结束信号
                            try:
                                ffmpeg_deblock_in_queue.put(None)
                            except Exception:
                                pass

                    feeder_thread = threading.Thread(target=_feeder_func, daemon=True)
                    feeder_thread.start()

                    def _handle_deblock_frame(frame_data: bytes):
                        final_bytes = frame_data
                        if need_black_restore_static:
                            tmp_img = Image.frombytes("RGB", (width, height), frame_data)
                            frame_img = _restore_transparent_from_black_rgb(tmp_img)
                            final_bytes = frame_img.tobytes()
                            try:
                                tmp_img.close()
                                frame_img.close()
                            except Exception:
                                pass
                        for key in list(ffmpeg_processes.keys()):
                            ok = _write_frame_bytes(key, final_bytes)
                            if ok is False:
                                return False
                        self.current_frame += 1
                        self.progress_signal.emit(self.current_frame, self.total_frames)
                        return True

                    wait_start = None
                    while True:
                        if self.check_pause_stop():
                            # 需要清理 Feeder
                            # 这里只要 break，下面的 shutdown 会处理
                            break
                        
                        try:
                            # 从 Deblock 获取处理后的帧
                            # 增加 timeout 以便响应暂停/停止
                            frame_data = ffmpeg_deblock_queue.get(timeout=0.1)
                        except queue.Empty:
                            # 如果 Feeder 死了且队列空了，说明结束
                            if not feeder_thread.is_alive() and ffmpeg_deblock_queue.empty():
                                with ffmpeg_deblock_lock:
                                    sent = ffmpeg_deblock_sent[0]
                                    received = ffmpeg_deblock_received[0]
                                if received < sent:
                                    if wait_start is None:
                                        wait_start = time.monotonic()
                                    if ffmpeg_deblock_proc is not None and ffmpeg_deblock_proc.poll() is not None:
                                        self.log_signal.emit(tr('  FFmpeg 去块丢帧，准备回退: 已发送 {0} / 已接收 {1}', sent, received))
                                        retry_with_internal = True
                                        retry_received = received
                                        skip_ffmpeg_log = True
                                        break
                                    if time.monotonic() - wait_start < 10.0:
                                        continue
                                    self.log_signal.emit(tr('  FFmpeg 去块输出等待超时，准备回退: 已发送 {0} / 已接收 {1}', sent, received))
                                    retry_with_internal = True
                                    retry_received = received
                                    skip_ffmpeg_log = True
                                    break
                                break
                            # 检查错误
                            exc = ffmpeg_deblock_exc.get("exc") if "ffmpeg_deblock_exc" in locals() else None
                            if exc:
                                raise exc
                            continue
                        
                        if frame_data is None:
                            with ffmpeg_deblock_lock:
                                sent = ffmpeg_deblock_sent[0]
                                received = ffmpeg_deblock_received[0]
                            if received < sent:
                                if wait_start is None:
                                    wait_start = time.monotonic()
                                if ffmpeg_deblock_proc is not None and ffmpeg_deblock_proc.poll() is not None:
                                    self.log_signal.emit(tr('  FFmpeg 去块丢帧，准备回退: 已发送 {0} / 已接收 {1}', sent, received))
                                    retry_with_internal = True
                                    retry_received = received
                                    skip_ffmpeg_log = True
                                    break
                                if time.monotonic() - wait_start < 10.0:
                                    continue
                                self.log_signal.emit(tr('  FFmpeg 去块输出等待超时，准备回退: 已发送 {0} / 已接收 {1}', sent, received))
                                retry_with_internal = True
                                retry_received = received
                                skip_ffmpeg_log = True
                                break
                            break
                        wait_start = None
                        if _handle_deblock_frame(frame_data) is False:
                            break

                    # 等待 Feeder 结束
                    feeder_thread.join(timeout=1.0)
                
                else:
                    # ==========================================
                    # 原有同步循环模式
                    # ==========================================
                    for i, offset, _, frame_data in iter_frame_bytes(f, frame_offsets, file_size):
                        if self.check_pause_stop():
                            if ffmpeg_processes:
                                try:
                                    for p in ffmpeg_processes.values():
                                        p.terminate()
                                except Exception:
                                    pass
                            if ffmpeg_deblock_proc is not None:
                                try:
                                    ffmpeg_deblock_proc.terminate()
                                except Exception:
                                    pass
                            return

                        # 解码帧
                        img_bytes = None
                        need_black_restore = need_black_restore_static
                        if self.use_c_decoder:
                            # 使用 C 解码
                            try:
                                img_bytes = decode_frame_c(frame_data, width, height, 32, quant_matrices, prev_bytes)
                                prev_bytes = img_bytes
                                img = None
                                if (not self.export_video) or need_black_restore:
                                    img = Image.frombuffer('RGBA', (width, height), img_bytes, 'raw', 'RGBA', 0, 1)
                            except Exception as e:
                                self.log_signal.emit(tr('  C 解码失败，尝试 Python 回退: {0}', e))
                                # 回退到 Python 解码
                                if decoder is None:
                                    decoder = ParallelCbgDecoder(width, height, 32, quant_matrices)
                                decode_frame(decoder, frame_data, width, height)
                                img = build_image(decoder, width, height)
                                prev_bytes = img.tobytes()
                        else:
                            # 使用 Python 解码
                            decode_frame(decoder, frame_data, width, height)
                            img = build_image(decoder, width, height)

                        if not self.export_video and first_png_mode is None:
                            first_png_mode = img.mode
                        if use_black_bg_32 and not self.export_video and all_alpha_opaque:
                            if img_bytes is not None:
                                if not _alpha_is_fully_opaque_bytes(img_bytes):
                                    all_alpha_opaque = False
                            else:
                                if not _alpha_is_fully_opaque_image(img):
                                    all_alpha_opaque = False

                        if self.export_video:
                            if ffmpeg_processes:
                                if need_black_restore:
                                    src_bytes = img_bytes if img_bytes is not None else img.tobytes()
                                    if img is not None:
                                        try:
                                            img.close()
                                        except Exception:
                                            pass
                                    if use_ffmpeg_deblock:
                                        src_bytes = _ffmpeg_deblock_bytes(src_bytes)
                                    elif deblock_enabled_runtime:
                                        src_bytes = _apply_deblock_rgba_bytes(src_bytes, width, height, deblock_method, deblock_strength)
                                    if use_ffmpeg_deblock and ffmpeg_deblock_mode == "rgb":
                                        tmp_img = Image.frombytes("RGB", (width, height), src_bytes)
                                    else:
                                        tmp_img = Image.frombytes("RGBA", (width, height), src_bytes)
                                    frame_img = _restore_transparent_from_black_rgb(tmp_img)
                                    frame_bytes = frame_img.tobytes()
                                    try:
                                        tmp_img.close()
                                    except Exception:
                                        pass
                                    try:
                                        frame_img.close()
                                    except Exception:
                                        pass
                                else:
                                    src_bytes = img_bytes if img_bytes is not None else img.tobytes()
                                    if use_ffmpeg_deblock:
                                        frame_bytes = _ffmpeg_deblock_bytes(src_bytes)
                                    elif deblock_enabled_runtime:
                                        frame_bytes = _apply_deblock_rgba_bytes(src_bytes, width, height, deblock_method, deblock_strength)
                                    else:
                                        frame_bytes = src_bytes
                                    if img is not None:
                                        try:
                                            img.close()
                                        except Exception:
                                            pass
                                for key in list(ffmpeg_processes.keys()):
                                    ok = _write_frame_bytes(key, frame_bytes)
                                    if ok is False:
                                        return
                        else:
                            save_path = png_pattern % i
                            inline_black_bg = bool(force_black_bg or use_black_bg_24) and (not use_ffmpeg_deblock)
                            frame_img = img
                            frame_bytes = None
                            if inline_black_bg:
                                src_bytes = img_bytes if img_bytes is not None else frame_img.tobytes()
                                if frame_img is not None:
                                    try:
                                        frame_img.close()
                                    except Exception:
                                        pass
                                if deblock_enabled_runtime:
                                    src_bytes = _apply_deblock_rgba_bytes(src_bytes, width, height, deblock_method, deblock_strength)
                                tmp_img = Image.frombytes("RGBA", (width, height), src_bytes)
                                out_img = _restore_transparent_from_black_rgb(tmp_img)
                                frame_bytes = out_img.tobytes()
                                try:
                                    tmp_img.close()
                                except Exception:
                                    pass
                                try:
                                    out_img.close()
                                except Exception:
                                    pass
                                frame_img = None
                            elif deblock_enabled_runtime:
                                src_bytes = img_bytes if img_bytes is not None else frame_img.tobytes()
                                if frame_img is not None:
                                    try:
                                        frame_img.close()
                                    except Exception:
                                        pass
                                frame_bytes = _apply_deblock_rgba_bytes(src_bytes, width, height, deblock_method, deblock_strength)
                                frame_img = None
                            if executor is not None:
                                if self.enable_multiprocess:
                                    if frame_bytes is None:
                                        frame_bytes = frame_img.tobytes()
                                        frame_img.close()
                                    pending.append(executor.submit(_save_png_bytes, save_path, width, height, frame_bytes))
                                else:
                                    if frame_img is None:
                                        frame_img = Image.frombytes("RGBA", (width, height), frame_bytes)
                                    pending.append(executor.submit(_save_png, frame_img, save_path))
                                if len(pending) >= max_pending:
                                    _drain_pending(FIRST_COMPLETED)
                            else:
                                if frame_img is None:
                                    frame_img = Image.frombytes("RGBA", (width, height), frame_bytes)
                                frame_img.save(save_path)
                                frame_img.close()

                        self.current_frame += 1
                        self.progress_signal.emit(self.current_frame, self.total_frames)

                if pending:
                    _drain_pending(ALL_COMPLETED)

                if not self.export_video:
                    if use_ffmpeg_deblock:
                        try:
                            self.log_signal.emit(tr('  正在使用 FFmpeg 去块处理 PNG 序列...'))
                            startupinfo = None
                            if os.name == 'nt':
                                startupinfo = subprocess.STARTUPINFO()
                                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                            tmp_dir = os.path.join(out_dir, "_ffmpeg_deblock_tmp")
                            os.makedirs(tmp_dir, exist_ok=True)
                            out_pattern = os.path.join(tmp_dir, f"{base_name}_frame_%04d.png")
                            vf = _build_ffmpeg_deblock_vf("rgba")
                            cmd = [
                                self.ffmpeg_path, '-y',
                                '-start_number', '0',
                                '-i', png_pattern,
                                '-vf', vf,
                                '-pix_fmt', 'rgba',
                                out_pattern
                            ]
                            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)
                            if p.returncode != 0:
                                self.log_signal.emit(tr('  FFmpeg 去块失败，已跳过: {0}', p.stderr.decode('utf-8', errors='ignore')))
                            else:
                                replaced = 0
                                out_files = sorted(glob.glob(os.path.join(tmp_dir, f"{base_name}_frame_*.png")))
                                for src in out_files:
                                    if self.check_pause_stop():
                                        return
                                    dst = os.path.join(out_dir, os.path.basename(src))
                                    try:
                                        os.replace(src, dst)
                                        replaced += 1
                                    except Exception:
                                        pass
                                try:
                                    for p2 in glob.glob(os.path.join(tmp_dir, "*.png")):
                                        try:
                                            os.remove(p2)
                                        except Exception:
                                            pass
                                    os.rmdir(tmp_dir)
                                except Exception:
                                    pass
                                self.log_signal.emit(tr('  FFmpeg 去块完成: {0} 帧', replaced))
                        except FileNotFoundError:
                            self.log_signal.emit(tr('  错误: 未找到 ffmpeg，无法进行去块处理。'))
                        except Exception as e:
                            self.log_signal.emit(tr('  FFmpeg 去块失败，已跳过: {0}', e))

                    force_black_bg = False
                    if getattr(self, "force_black_bg_files", None):
                        force_black_bg = os.path.normcase(os.path.abspath(filepath)) in self.force_black_bg_files

                    inline_black_bg = bool(force_black_bg or use_black_bg_24) and (not use_ffmpeg_deblock)
                    apply_black_bg_png = (force_black_bg or use_black_bg_24 or (use_black_bg_32 and all_alpha_opaque)) and (not inline_black_bg)
                    if not force_black_bg:
                        if remove_black_bg_24 and header_png_bit_depth != 24:
                            self.log_signal.emit(tr('  去黑底跳过: 仅支持文件头位深24 (当前 {0})', header_bd_str))
                            apply_black_bg_png = False
                        if use_black_bg_32 and not all_alpha_opaque:
                            self.log_signal.emit(tr('  32bpp 含透明像素，去黑底跳过'))
                            apply_black_bg_png = False
                        if inline_black_bg:
                            self.log_signal.emit(tr('  去黑底已在导出时完成 (文件头位深24)'))
                    else:
                        if inline_black_bg:
                            self.log_signal.emit(tr('  自定义去黑底文件: 导出时已去黑底'))
                        else:
                            self.log_signal.emit(tr('  自定义去黑底文件: 将在导出后去黑底'))

                    if apply_black_bg_png:
                        if force_black_bg:
                            self.log_signal.emit(tr('  开始去除黑色背景 (自定义文件)...'))
                        elif use_black_bg_24:
                            self.log_signal.emit(tr('  开始去除黑色背景 (文件头位深24)...'))
                        else:
                            self.log_signal.emit(tr('  开始去除黑色背景 (32bpp 全不透明)...'))
                        pattern = os.path.join(out_dir, f"{base_name}_frame_*.png")
                        files = sorted(glob.glob(pattern))
                        processed = 0
                        if files:
                            # 优化: 去黑底不增加总进度，改用状态提示
                            if hasattr(self, "status_signal"):
                                self.status_signal.emit(tr('正在去除黑底: 0/{0}', len(files)))
                        if executor is not None:
                            pending = []
                            future_to_path = {}
                            last_emit = 0.0
                            for p in files:
                                if self.check_pause_stop():
                                    return
                                if self.enable_multiprocess:
                                    fut = executor.submit(_fix_png_file, p)
                                else:
                                    fut = executor.submit(_fix_png, p)
                                pending.append(fut)
                                future_to_path[fut] = p
                                if len(pending) >= max_pending:
                                    while True:
                                        done, not_done = wait(pending, return_when=FIRST_COMPLETED, timeout=0.5)
                                        if not done:
                                            if self.check_pause_stop():
                                                for fut in not_done:
                                                    fut.cancel()
                                                return
                                            continue
                                        for fut in done:
                                            try:
                                                fut.result()
                                                processed += 1
                                                now = time.monotonic()
                                                if hasattr(self, "status_signal") and (processed == len(files) or processed % 25 == 0 or (now - last_emit) >= 0.25):
                                                    last_emit = now
                                                    self.status_signal.emit(tr('正在去除黑底: {0}/{1}', processed, len(files)))
                                            except Exception as e:
                                                self.log_signal.emit(tr('  去黑底失败: {0}: {1}', os.path.basename(future_to_path.get(fut, 'unknown')), e))
                                            future_to_path.pop(fut, None)
                                        pending = list(not_done)
                                        break
                            while pending:
                                done, not_done = wait(pending, return_when=FIRST_COMPLETED, timeout=0.5)
                                if not done:
                                    if self.check_pause_stop():
                                        for fut in not_done:
                                            fut.cancel()
                                        return
                                    continue
                                for fut in done:
                                    try:
                                        fut.result()
                                        processed += 1
                                        now = time.monotonic()
                                        if hasattr(self, "status_signal") and (processed == len(files) or processed % 25 == 0 or (now - last_emit) >= 0.25):
                                            last_emit = now
                                            self.status_signal.emit(tr('正在去除黑底: {0}/{1}', processed, len(files)))
                                    except Exception as e:
                                        self.log_signal.emit(tr('  去黑底失败: {0}: {1}', os.path.basename(future_to_path.get(fut, 'unknown')), e))
                                    future_to_path.pop(fut, None)
                                pending = list(not_done)
                        else:
                            last_emit = 0.0
                            for p in files:
                                if self.check_pause_stop():
                                    return
                                try:
                                    _fix_png(p)
                                    processed += 1
                                    now = time.monotonic()
                                    if hasattr(self, "status_signal") and (processed == len(files) or processed % 25 == 0 or (now - last_emit) >= 0.25):
                                        last_emit = now
                                        self.status_signal.emit(tr('正在去除黑底: {0}/{1}', processed, len(files)))
                                except Exception as e:
                                    self.log_signal.emit(tr('  去黑底失败: {0}: {1}', os.path.basename(p), e))
                        self.log_signal.emit(tr('  去黑底完成: {0}/{1}', processed, len(files)))

            finally:
                # 首先通知 writer 线程结束
                for q in writer_queues.values():
                    try:
                        q.put(None)
                    except Exception:
                        pass

                # 必须等待 writer 线程真正完成写入，否则提前关闭 stdin 会导致数据丢失
                # 尤其是在高负载下，写入可能会变慢
                for t in writer_threads.values():
                    try:
                        # 循环等待，直到线程结束，或者用户强制停止
                        while t.is_alive():
                            t.join(timeout=0.5)
                            if self.check_pause_stop():
                                break
                    except Exception:
                        pass

                # 只有在 writer 线程结束后，才安全关闭 stdin
                for p in ffmpeg_processes.values():
                    if p and p.stdin:
                        try:
                            # 确保刷新缓冲区
                            p.stdin.flush()
                            p.stdin.close()
                        except Exception:
                            pass
                for t in ffmpeg_stderr_threads.values():
                    try:
                        while t.is_alive():
                            t.join(timeout=0.5)
                            if self.check_pause_stop():
                                break
                    except Exception:
                        pass
                if ffmpeg_deblock_proc is not None:
                    try:
                        if "ffmpeg_deblock_in_queue" in locals() and ffmpeg_deblock_in_queue is not None:
                            try:
                                ffmpeg_deblock_in_queue.put(None, timeout=0.2)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    try:
                        if "ffmpeg_deblock_writer_thread" in locals() and ffmpeg_deblock_writer_thread is not None:
                            ffmpeg_deblock_writer_thread.join(timeout=1)
                    except Exception:
                        pass
                    try:
                        if "ffmpeg_deblock_queue" in locals() and ffmpeg_deblock_queue is not None and "ffmpeg_deblock_lock" in locals():
                            start = time.monotonic()
                            while True:
                                with ffmpeg_deblock_lock:
                                    sent = ffmpeg_deblock_sent[0]
                                    received = ffmpeg_deblock_received[0]
                                if received >= sent and ffmpeg_deblock_queue.empty():
                                    break
                                if ffmpeg_deblock_proc is not None and ffmpeg_deblock_proc.poll() is not None:
                                    break
                                if time.monotonic() - start >= 2.0:
                                    break
                                time.sleep(0.05)
                            if received < sent:
                                self.log_signal.emit(tr('  FFmpeg 去块关闭等待超时，可能丢帧: 已发送 {0} / 已接收 {1}', sent, received))
                    except Exception:
                        pass
                    try:
                        if ffmpeg_deblock_proc.stdin:
                            ffmpeg_deblock_proc.stdin.close()
                    except Exception:
                        pass
                    try:
                        ffmpeg_deblock_proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        try:
                            ffmpeg_deblock_proc.terminate()
                        except Exception:
                            pass
                        try:
                            ffmpeg_deblock_proc.wait(timeout=2)
                        except Exception:
                            pass
                    except Exception:
                        pass
                    try:
                        if "ffmpeg_deblock_thread" in locals() and ffmpeg_deblock_thread is not None:
                            ffmpeg_deblock_thread.join(timeout=1)
                    except Exception:
                        pass
                    try:
                        if "ffmpeg_deblock_writer_thread" in locals() and ffmpeg_deblock_writer_thread is not None:
                            ffmpeg_deblock_writer_thread.join(timeout=1)
                    except Exception:
                        pass
                    try:
                        if "ffmpeg_deblock_err_thread" in locals() and ffmpeg_deblock_err_thread is not None:
                            ffmpeg_deblock_err_thread.join(timeout=1)
                    except Exception:
                        pass
                if executor is not None and executor is not shared_executor:
                    try:
                        executor.shutdown(wait=True, cancel_futures=True)
                    except TypeError:
                        executor.shutdown(wait=True)

                for key, p in ffmpeg_processes.items():
                    try:
                        p.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        try:
                            p.terminate()
                        except Exception:
                            pass
                        try:
                            p.wait(timeout=2)
                        except Exception:
                            pass
                    err = bytes(ffmpeg_stderr_tails.get(key) or b"")
                    out_path = output_files.get(key)
                    if skip_ffmpeg_log:
                        continue
                    if p.returncode != 0:
                        self.log_signal.emit(tr('  视频生成失败({0}): {1}', key, err.decode('utf-8', errors='ignore')))
                    else:
                        self.log_signal.emit(tr('  视频已生成({0}): {1}', key, out_path))
            if retry_with_internal and (not force_internal_deblock):
                if retry_received:
                    self.current_frame = max(0, int(self.current_frame) - int(retry_received))
                    try:
                        self.progress_signal.emit(self.current_frame, self.total_frames)
                    except Exception:
                        pass
                self.log_signal.emit(tr('  FFmpeg 去块出现缺帧，已切换为双边滤波重新处理'))
                return self.process_file(filepath, shared_executor, True)


class _SignalProxy:
    def __init__(self, emit_func):
        self.emit = emit_func


def _process_file_in_subprocess(filepath, config, log_queue, progress_queue, stop_event, pause_event, total_frames):
    file_tag = os.path.splitext(os.path.basename(filepath))[0]
    last_current = [0]

    def _log_emit(msg):
        if log_queue is not None:
            log_queue.put(msg)

    def _progress_emit(current, total):
        delta = current - last_current[0]
        last_current[0] = current
        if delta and progress_queue is not None:
            progress_queue.put(("p", file_tag, int(delta)))

    def _emit_total_delta(delta_total):
        if progress_queue is not None and delta_total:
            progress_queue.put(("t", file_tag, int(delta_total)))

    def _status_emit(msg):
        if progress_queue is not None:
            progress_queue.put(("s", file_tag, str(msg)))

    def _check_pause_stop():
        if stop_event is not None and stop_event.is_set():
            return True
        while pause_event is not None and pause_event.is_set():
            if stop_event is not None and stop_event.is_set():
                return True
            time.sleep(0.05)
        return stop_event is not None and stop_event.is_set()

    use_c_decoder = bool(config.get("use_c_decoder", False)) and (_load_decoder_lib() is not None)
    if use_c_decoder:
        _set_c_decoder_threads(config.get("thread_count", 0))

    worker = types.SimpleNamespace(
        input_path=filepath,
        output_path=config.get("output_path"),
        export_video=config.get("export_video"),
        video_format=config.get("video_format"),
        use_custom_fps=config.get("use_custom_fps"),
        custom_fps=config.get("custom_fps"),
        crf_value=config.get("crf_value"),
        ffmpeg_path=config.get("ffmpeg_path"),
        remove_black_bg_24bit=config.get("remove_black_bg_24bit"),
        remove_black_bg_32_opaque=config.get("remove_black_bg_32_opaque"),
        enable_deblock=config.get("enable_deblock", False),
        deblock_method=config.get("deblock_method", "bilateral"),
        deblock_strength=config.get("deblock_strength", 10),
        force_black_bg_files=set(config.get("force_black_bg_files") or []),
        enable_multithread=config.get("enable_multithread"),
        enable_multiprocess=config.get("enable_multiprocess"),
        thread_count=config.get("thread_count"),
        export_color_mask=config.get("export_color_mask"),
        export_side_mask=config.get("export_side_mask", False),
        keep_original_video=config.get("keep_original_video"),
        total_frames=total_frames,
        current_frame=0,
        use_c_decoder=use_c_decoder,
        log_signal=_SignalProxy(_log_emit),
        progress_signal=_SignalProxy(_progress_emit),
        status_signal=_SignalProxy(_status_emit),
        emit_total_delta=_emit_total_delta,
        check_pause_stop=_check_pause_stop
    )

    try:
        worker.process_file = types.MethodType(Worker.process_file, worker)
        setattr(worker, "process file", worker.process_file)
    except Exception:
        pass
    worker.process_file(filepath, None)


class SpeedBenchmarkWorker(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)
    progress_signal = pyqtSignal(int, int)

    def __init__(self, input_path, frame_limit=60):
        super().__init__()
        self.input_path = input_path
        self.frame_limit = max(1, int(frame_limit))
        self._is_paused = False
        self._is_stopped = False
        self._pause_cond = QWaitCondition()
        self._mutex = QMutex()

    def set_paused(self, paused):
        self._mutex.lock()
        self._is_paused = paused
        if not paused:
            self._pause_cond.wakeAll()
        self._mutex.unlock()

    def stop(self):
        self._mutex.lock()
        self._is_stopped = True
        self._is_paused = False
        self._pause_cond.wakeAll()
        self._mutex.unlock()

    def check_pause_stop(self):
        self._mutex.lock()
        if self._is_stopped:
            self._mutex.unlock()
            return True
        
        if self._is_paused:
            self._pause_cond.wait(self._mutex)
        
        if self._is_stopped:
            self._mutex.unlock()
            return True
            
        self._mutex.unlock()
        return False

    def run(self):
        try:
            bm_files = []
            if os.path.isfile(self.input_path):
                bm_files = [self.input_path]
            elif os.path.isdir(self.input_path):
                for root, _, files in os.walk(self.input_path):
                    for file in files:
                        if file.lower().endswith(".bm"):
                            bm_files.append(os.path.join(root, file))
            else:
                self.finished_signal.emit(False, tr('输入路径不存在'))
                return

            if not bm_files:
                self.finished_signal.emit(False, tr('未找到 BM 文件'))
                return

            bm_files.sort()
            total_files = len(bm_files)
            self.log_signal.emit(tr('检测到 {0} 个 BM 文件，开始测速', total_files))

            c_available = (_load_decoder_lib() is not None)
            if not c_available:
                self.log_signal.emit(tr('C 解码库不可用，跳过 C 测试'))

            if not HAS_OPTIMIZATION:
                self.log_signal.emit(tr('未检测到 Numpy/Numba，加速 Python 退化为纯 Python'))

            file_metas = []
            total_frames = 0
            for bm_path in bm_files:
                if self.check_pause_stop():
                    self.finished_signal.emit(False, tr('测速已停止'))
                    return

                file_name = os.path.basename(bm_path)
                with open(bm_path, 'rb', buffering=1024 * 1024) as f:
                    magic = f.read(16)
                    if b'BF_Movie' not in magic:
                        self.log_signal.emit(tr('{0}: 非 BF_Movie 文件，跳过', file_name))
                        continue

                    f.seek(0x14)
                    width = struct.unpack('<I', f.read(4))[0]
                    f.seek(0x18)
                    height = struct.unpack('<I', f.read(4))[0]
                    f.seek(0x40)
                    quant_matrices = f.read(128)
                    frame_offsets = read_frame_offsets(f)

                    if not frame_offsets:
                        self.log_signal.emit(tr('{0}: 文件中没有帧数据，跳过', file_name))
                        continue

                    sample_count = min(self.frame_limit, len(frame_offsets))
                    f.seek(0, 2)
                    file_size = f.tell()

                if sample_count <= 0:
                    self.log_signal.emit(tr('{0}: 取样帧数为 0，跳过', file_name))
                    continue

                file_metas.append({
                    "path": bm_path,
                    "name": file_name,
                    "width": width,
                    "height": height,
                    "quant": quant_matrices,
                    "offsets": frame_offsets,
                    "sample_count": sample_count,
                    "file_size": file_size
                })
                total_frames += sample_count

            if total_frames <= 0:
                self.finished_signal.emit(False, tr('没有可测速的帧'))
                return

            self.progress_signal.emit(0, total_frames)

            total_c_time = 0.0
            total_py_time = 0.0
            total_c_frames = 0
            total_py_frames = 0
            done_frames = 0

            for meta in file_metas:
                if self.check_pause_stop():
                    self.finished_signal.emit(False, tr('测速已停止'))
                    return

                file_name = meta["name"]
                width = meta["width"]
                height = meta["height"]
                quant_matrices = meta["quant"]
                offsets = meta["offsets"]
                sample_count = meta["sample_count"]
                file_size = meta["file_size"]

                self.log_signal.emit(tr('{0}: 分辨率 {1}x{2}, 取样帧数 {3}', file_name, width, height, sample_count))

                c_time = 0.0
                py_time = 0.0
                prev_bytes = bytes(width * height * 4) if c_available else None
                decoder = ParallelCbgDecoder(width, height, 32, quant_matrices)

                with open(meta["path"], 'rb', buffering=1024 * 1024) as f:
                    for i, offset, length, frame_data in iter_frame_bytes(f, offsets, file_size, limit=sample_count):
                        if self.check_pause_stop():
                            self.finished_signal.emit(False, tr('测速已停止'))
                            return
                        if length <= 0:
                            continue

                        if c_available:
                            start = time.perf_counter()
                            prev_bytes = decode_frame_c(frame_data, width, height, 32, quant_matrices, prev_bytes)
                            c_time += time.perf_counter() - start

                        start = time.perf_counter()
                        decode_frame(decoder, frame_data, width, height)
                        py_time += time.perf_counter() - start

                        done_frames += 1
                        self.progress_signal.emit(done_frames, total_frames)

                if c_available:
                    fps = sample_count / c_time if c_time > 0 else 0.0
                    self.log_signal.emit(tr('{0}: C 解码 {1:.3f}s, {2:.2f} fps', file_name, c_time, fps))
                    total_c_time += c_time
                    total_c_frames += sample_count

                fps = sample_count / py_time if py_time > 0 else 0.0
                self.log_signal.emit(tr('{0}: Python 解码 {1:.3f}s, {2:.2f} fps', file_name, py_time, fps))
                total_py_time += py_time
                total_py_frames += sample_count

            if total_c_frames > 0:
                total_c_fps = total_c_frames / total_c_time if total_c_time > 0 else 0.0
                self.log_signal.emit(tr('总计: C 解码 {0:.3f}s, {1:.2f} fps', total_c_time, total_c_fps))
            if total_py_frames > 0:
                total_py_fps = total_py_frames / total_py_time if total_py_time > 0 else 0.0
                self.log_signal.emit(tr('总计: Python 解码 {0:.3f}s, {1:.2f} fps', total_py_time, total_py_fps))

            self.finished_signal.emit(True, tr('测速完成'))
        except Exception as e:
            import traceback
            self.log_signal.emit(traceback.format_exc())
            self.finished_signal.emit(False, str(e))


# ==========================================
# 图形用户界面
# ==========================================

def is_windows_dark_mode():
    if not HAS_WINREG or os.name != 'nt':
        return False
    try:
        registry = winreg.ConnectRegistry(None, winreg.HKEY_CURRENT_USER)
        key = winreg.OpenKey(registry, r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
        value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
        winreg.CloseKey(key)
        return value == 0
    except Exception:
        return False

from ..gui.paths import PathLineEdit


class DragDropLineEdit(PathLineEdit):
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls: return
        local_path = urls[0].toLocalFile()
        if local_path:
            ui_call(self, 'setText', local_path)
        event.acceptProposedAction()


class MultiFileDragDropLineEdit(PathLineEdit):
    multiple_paths = True
    def __init__(self, extensions=None):
        super().__init__()
        self.setAcceptDrops(True)
        self.extensions = [e.lower() for e in extensions] if extensions else None

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            return
        picked = []
        for url in urls:
            local_path = url.toLocalFile()
            if not local_path:
                continue
            if self.extensions and not any(local_path.lower().endswith(ext) for ext in self.extensions):
                continue
            picked.append(local_path)
        if not picked:
            return
        existing = [p.strip() for p in self.text().split(";") if p.strip()]
        merged = []
        seen = set()
        for p in existing + picked:
            key = os.path.normcase(os.path.abspath(p))
            if key in seen:
                continue
            seen.add(key)
            merged.append(p)
        ui_call(self, 'setText', ';'.join(merged))
        event.acceptProposedAction()

class ModernButton(QPushButton):
    def __init__(self, text, is_primary=False):
        super().__init__(text)
        ui_call(self, "setText", text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("PrimaryButton" if is_primary else "SecondaryButton")
        self.setMinimumHeight(32)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        ui_call(self, 'setWindowTitle', 'BM Converter')
        self.resize(900, 850)
        self.worker = None
        self.benchmark_worker = None
        self.ffmpeg_path = "ffmpeg"
        self._last_auto_output_path = ""
        self.init_ui()
        self.apply_theme()
        self.paused = False

    def init_ui(self):
        main_widget = QWidget()
        main_widget.setObjectName("MainBackground")
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)

        header_layout = QHBoxLayout()
        title = ui_widget(QLabel, 'BM Converter')
        title.setObjectName("AppTitle")
        
        self.theme_combo = QComboBox()
        ui_call(self.theme_combo, 'addItems', ['系统 (System)', '明亮 (Light)', '暗黑 (Dark)'])
        self.theme_combo.currentIndexChanged.connect(self.apply_theme)
        
        header_layout.addWidget(title)
        header_layout.addStretch()
        header_layout.addWidget(ui_widget(QLabel, tr('主题:')))
        header_layout.addWidget(self.theme_combo)
        layout.addLayout(header_layout)

        self.apply_theme()

        input_card = QFrame()
        input_card.setObjectName("CardFrame")
        input_layout = QVBoxLayout(input_card)
        
        input_row = QHBoxLayout()
        self.input_edit = DragDropLineEdit()
        ui_call(self.input_edit, 'setPlaceholderText', tr('拖拽 BM 文件或文件夹到此处...'))
        btn_in_file = ModernButton(tr('文件'))
        btn_in_file.setFixedWidth(70)
        btn_in_file.clicked.connect(self.browse_input_file)
        btn_in_dir = ModernButton(tr('文件夹'))
        btn_in_dir.setFixedWidth(70)
        btn_in_dir.clicked.connect(self.browse_input_folder)
        input_row.addWidget(self.input_edit)
        input_row.addWidget(btn_in_file)
        input_row.addWidget(btn_in_dir)
        self.input_edit.textChanged.connect(self.sync_output_path_from_input)
        
        input_layout.addWidget(ui_widget(QLabel, tr('输入路径:')))
        input_layout.addLayout(input_row)
        layout.addWidget(input_card)

        output_card = QFrame()
        output_card.setObjectName("CardFrame")
        output_layout = QVBoxLayout(output_card)
        
        output_row = QHBoxLayout()
        self.output_edit = DragDropLineEdit()
        ui_call(self.output_edit, 'setPlaceholderText', tr('选择输出保存位置...'))
        btn_out = ModernButton(tr('文件夹'))
        btn_out.setFixedWidth(70)
        btn_out.clicked.connect(self.browse_output_folder)
        output_row.addWidget(self.output_edit)
        output_row.addWidget(btn_out)
        
        output_layout.addWidget(ui_widget(QLabel, tr('输出目录:')))
        output_layout.addLayout(output_row)
        layout.addWidget(output_card)

        opt_card = QFrame()
        opt_card.setObjectName("CardFrame")
        opt_layout = QVBoxLayout(opt_card)
        opt_layout.addWidget(ui_widget(QLabel, tr('输出模式')))
        
        mode_row = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        
        self.radio_png = ui_widget(QRadioButton, tr('导出 PNG 序列'))
        self.radio_video = ui_widget(QRadioButton, tr('导出视频文件 (推荐)'))
        self.radio_video.setChecked(True)
        
        self.mode_group.addButton(self.radio_png)
        self.mode_group.addButton(self.radio_video)
        self.mode_group.buttonToggled.connect(self.toggle_mode)
        
        mode_row.addWidget(self.radio_png)
        mode_row.addWidget(self.radio_video)
        
        self.btn_ffmpeg = ModernButton(tr('选择FFmpeg'))
        ui_call(self.btn_ffmpeg, 'setToolTip', tr('默认使用系统环境变量，点击选择自定义路径'))
        self.btn_ffmpeg.setFixedSize(92, 32)
        self.btn_ffmpeg.clicked.connect(self.browse_ffmpeg)
        
        self.ffmpeg_edit = PathLineEdit("ffmpeg")
        self.ffmpeg_edit.setVisible(False)
        
        mode_row.addWidget(self.btn_ffmpeg)
        mode_row.addSpacing(10)
        mode_row.addWidget(ui_widget(QLabel, tr('并行方式:')))
        self.parallel_mode_combo = QComboBox()
        ui_call(self.parallel_mode_combo, 'addItems', [tr('自动 (推荐)'), tr('多线程'), tr('多进程'), tr('关闭')])
        self.parallel_mode_combo.setCurrentIndex(0)
        mode_row.addWidget(self.parallel_mode_combo)
        
        mode_row.addWidget(ui_widget(QLabel, tr('并行数量:')))
        self.thread_spin = QSpinBox()
        max_threads = max(1, os.cpu_count() or 1)
        self.thread_spin.setRange(0, max_threads)
        self.thread_spin.setSpecialValueText(tr('自动'))
        self.thread_spin.setValue(0)
        self.thread_spin.setFixedWidth(60)
        self.thread_spin.setEnabled(True)
        self.parallel_mode_combo.currentIndexChanged.connect(self.update_parallel_controls)
        mode_row.addWidget(self.thread_spin)
        mode_row.addSpacing(10)
        mode_row.addWidget(ui_widget(QLabel, tr('文件并行数(1=关闭):')))
        self.file_parallel_spin = QSpinBox()
        self.file_parallel_spin.setRange(0, max_threads)
        self.file_parallel_spin.setSpecialValueText(tr('自动'))
        self.file_parallel_spin.setValue(0)
        self.file_parallel_spin.setFixedWidth(60)
        self.file_parallel_spin.setEnabled(True)
        mode_row.addWidget(self.file_parallel_spin)
        mode_row.addStretch()
        opt_layout.addLayout(mode_row)
        self.update_parallel_controls()
        
        bg_row = QHBoxLayout()
        bg_row.addWidget(ui_widget(QLabel, tr('去黑底:')))
        self.remove_black_bg_checkbox = ui_widget(QCheckBox, '24bpp')
        self.remove_black_bg_checkbox.setChecked(False)
        bg_row.addWidget(self.remove_black_bg_checkbox)
        
        self.remove_black_bg_32_checkbox = ui_widget(QCheckBox, tr('32bpp 且全不透明'))
        self.remove_black_bg_32_checkbox.setChecked(False)
        bg_row.addWidget(self.remove_black_bg_32_checkbox)
        bg_row.addWidget(ui_widget(QLabel, tr('自定义去黑底BM:')))
        self.custom_black_bg_edit = MultiFileDragDropLineEdit(extensions=[".bm"])
        ui_call(self.custom_black_bg_edit, 'setPlaceholderText', tr('拖拽BM文件(可多选，;分隔)'))
        self.custom_black_bg_edit.setMinimumWidth(220)
        bg_row.addWidget(self.custom_black_bg_edit)
        self.custom_black_bg_btn = ModernButton(tr('选择BM'))
        self.custom_black_bg_btn.setFixedWidth(72)
        self.custom_black_bg_btn.clicked.connect(self.browse_custom_black_bg_files)
        bg_row.addWidget(self.custom_black_bg_btn)
        bg_row.addStretch()
        opt_layout.addLayout(bg_row)

        deblock_row = QHBoxLayout()
        deblock_row.addWidget(ui_widget(QLabel, tr('去噪点/块效应:')))
        self.deblock_checkbox = ui_widget(QCheckBox, tr('启用'))
        self.deblock_checkbox.setChecked(False)
        deblock_row.addWidget(self.deblock_checkbox)

        deblock_row.addWidget(ui_widget(QLabel, tr('算法:')))
        self.deblock_method_combo = QComboBox()
        ui_call(self.deblock_method_combo, 'addItem', tr('FFmpeg 去块'), 'ffmpeg')
        ui_call(self.deblock_method_combo, 'addItem', tr('双边滤波(推荐)'), 'bilateral')
        ui_call(self.deblock_method_combo, 'addItem', tr('非局部均值'), 'nlmeans')
        ui_call(self.deblock_method_combo, 'addItem', tr('中值滤波'), 'median')
        ui_call(self.deblock_method_combo, 'addItem', tr('高斯模糊'), 'gaussian')
        if HAS_CV2 and getattr(cv2, "edgePreservingFilter", None) is not None:
            ui_call(self.deblock_method_combo, 'addItem', tr('边缘保持'), 'edgepreserve')
        bm3d_available = HAS_CV2 and getattr(getattr(cv2, "xphoto", None), "bm3dDenoising", None) is not None
        ui_call(self.deblock_method_combo, 'addItem', tr('BM3D(很慢)') if bm3d_available else tr('BM3D(需opencv-contrib)'), 'bm3d')
        if np is not None:
            ui_call(self.deblock_method_combo, 'addItem', tr('WNNM(极慢)'), 'wnnm')
        self.deblock_method_combo.setCurrentIndex(1)
        self.deblock_method_combo.setEnabled(False)
        deblock_row.addWidget(self.deblock_method_combo)

        deblock_row.addWidget(ui_widget(QLabel, tr('强度:')))
        self.deblock_strength_spin = QSpinBox()
        self.deblock_strength_spin.setRange(1, 50)
        self.deblock_strength_spin.setValue(10)
        self.deblock_strength_spin.setFixedWidth(60)
        self.deblock_strength_spin.setEnabled(False)
        deblock_row.addWidget(self.deblock_strength_spin)
        self.deblock_strength_desc = ui_widget(QLabel, '')
        self.deblock_strength_desc.setStyleSheet("color: #666; font-size: 9pt;")
        deblock_row.addWidget(self.deblock_strength_desc)
        deblock_row.addStretch()
        opt_layout.addLayout(deblock_row)

        def _update_deblock_strength_desc():
            method = ""
            if getattr(self, "deblock_method_combo", None) is not None:
                data = self.deblock_method_combo.currentData()
                if isinstance(data, str):
                    method = data
            mapping = {
                "ffmpeg": tr('FFmpeg：1-50，越大越强（建议 6-14）'),
                "bilateral": tr('双边：1-50，越大越平滑（建议 6-20）'),
                "nlmeans": tr('NLM：1-50，越大去噪更强（建议 8-18）'),
                "median": tr('中值：1-50，越大更强（会糊细节）（建议 6-15）'),
                "gaussian": tr('高斯：1-50，越大更糊（建议 4-12）'),
                "edgepreserve": tr('边缘保持：1-50，越大更强（建议 6-18）'),
                "bm3d": tr('BM3D：1-50，越大去噪更强（很慢，建议 6-14）'),
                "wnnm": tr('WNNM：1-50，越大更强（极慢，建议 6-12）'),
            }
            text = mapping.get(method, "1-50")
            ui_call(self.deblock_strength_desc, 'setText', text)
            ui_call(self.deblock_strength_spin, 'setToolTip', text)

        def _toggle_deblock_controls(checked: bool):
            self.deblock_method_combo.setEnabled(bool(checked))
            self.deblock_strength_spin.setEnabled(bool(checked))
            self.deblock_strength_desc.setEnabled(bool(checked))
            _update_deblock_strength_desc()

        self.deblock_checkbox.toggled.connect(_toggle_deblock_controls)
        self.deblock_method_combo.currentIndexChanged.connect(_update_deblock_strength_desc)
        _update_deblock_strength_desc()
        _toggle_deblock_controls(self.deblock_checkbox.isChecked())
        
        self.video_opts_frame = QFrame()
        video_layout = QVBoxLayout(self.video_opts_frame)
        video_layout.setContentsMargins(0, 10, 0, 0)
        video_layout.setSpacing(10)
        
        self.format_combo = QComboBox()
        ui_call(self.format_combo, 'addItems', ['WebM (VP9, Alpha)', 'MOV (ProRes 4444, Alpha)', 'APNG (Animated PNG)', 'MP4 (H.264, No Alpha)', 'MPG (MPEG-1, No Alpha)'])
        self.format_combo.setCurrentIndex(0)
        
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 144)
        self.fps_spin.setValue(30)
        self.fps_spin.setVisible(False)
        
        self.fps_mode = QComboBox()
        ui_call(self.fps_mode, 'addItems', [tr('自动 (从文件读取)'), tr('手动指定')])
        self.fps_mode.currentIndexChanged.connect(self.toggle_fps_spin)

        row1 = QHBoxLayout()
        row1.addWidget(ui_widget(QLabel, tr('视频格式:')))
        row1.addWidget(self.format_combo)
        row1.addSpacing(15)
        row1.addWidget(ui_widget(QLabel, tr('帧率:')))
        row1.addWidget(self.fps_mode)
        row1.addWidget(self.fps_spin)
        row1.addSpacing(10)
        self.renpy_enable_checkbox = ui_widget(QCheckBox, tr("生成色彩/遮罩 (Ren'Py)"))
        ui_call(self.renpy_enable_checkbox, 'setToolTip', tr("启用 Ren'Py 视频输出：侧边遮罩单视频 或 色彩/遮罩双视频"))
        self.renpy_enable_checkbox.setChecked(False)
        row1.addWidget(self.renpy_enable_checkbox)

        self.renpy_output_combo = QComboBox()
        ui_call(self.renpy_output_combo, 'addItem', tr('侧边遮罩(单视频)'), 'side')
        ui_call(self.renpy_output_combo, 'addItem', tr('色彩/遮罩(双视频)'), 'dual')
        self.renpy_output_combo.setCurrentIndex(0)
        self.renpy_output_combo.setEnabled(False)
        self.renpy_output_combo.setFixedWidth(120)
        row1.addWidget(self.renpy_output_combo)

        self.keep_original_video_checkbox = ui_widget(QCheckBox, tr('保留原始视频'))
        self.keep_original_video_checkbox.setChecked(False)
        self.keep_original_video_checkbox.setEnabled(False)
        row1.addWidget(self.keep_original_video_checkbox)
        row1.addStretch()
        video_layout.addLayout(row1)

        def _toggle_renpy_controls(checked: bool):
            self.renpy_output_combo.setEnabled(bool(checked))
            self.keep_original_video_checkbox.setEnabled(bool(checked))
            if not checked:
                self.keep_original_video_checkbox.setChecked(False)

        self.renpy_enable_checkbox.toggled.connect(_toggle_renpy_controls)
        _toggle_renpy_controls(self.renpy_enable_checkbox.isChecked())

        self.crf_label = ui_widget(QLabel, tr('质量 (CRF 0-63, 越小越好，建议范围 15-30。0为无损，51为极低画质。):'))
        self.crf_slider = QSlider(Qt.Orientation.Horizontal)
        self.crf_slider.setRange(0, 63)
        self.crf_slider.setValue(20)
        
        self.crf_spin = QSpinBox()
        self.crf_spin.setRange(0, 63)
        self.crf_spin.setValue(20)
        
        self.crf_slider.valueChanged.connect(self.crf_spin.setValue)
        self.crf_spin.valueChanged.connect(self.crf_slider.setValue)

        row3 = QHBoxLayout()
        row3.addWidget(self.crf_slider)
        row3.addWidget(self.crf_spin)

        video_layout.addWidget(self.crf_label)
        video_layout.addLayout(row3)
        video_layout.addStretch()
        
        opt_layout.addWidget(self.video_opts_frame)

        layout.addWidget(opt_card)

        status_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.status_label = ui_widget(QLabel, tr('准备就绪'))
        status_row.addWidget(self.status_label)
        status_row.addWidget(self.progress_bar)
        layout.addLayout(status_row)

        action_layout = QHBoxLayout()
        
        self.start_btn = ModernButton(tr('开始转换'), is_primary=True)
        self.start_btn.setFixedHeight(45)
        self.start_btn.clicked.connect(self.start_conversion)
        
        self.pause_btn = ModernButton(tr('暂停'), is_primary=False)
        self.pause_btn.setFixedHeight(45)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.pause_btn.setEnabled(False)
        
        self.stop_btn = ModernButton(tr('停止'), is_primary=False)
        self.stop_btn.setFixedHeight(45)
        self.stop_btn.setStyleSheet("QPushButton { color: #e74c3c; border-color: #e74c3c; } QPushButton:hover { background-color: #ffebee; }")
        self.stop_btn.clicked.connect(self.stop_conversion)
        self.stop_btn.setEnabled(False)

        self.benchmark_btn = ModernButton(tr('速度对比'), is_primary=False)
        self.benchmark_btn.setFixedHeight(45)
        self.benchmark_btn.clicked.connect(self.start_benchmark)
        
        action_layout.addWidget(self.stop_btn, 1)
        action_layout.addWidget(self.pause_btn, 1)
        action_layout.addWidget(self.benchmark_btn, 1)
        action_layout.addWidget(self.start_btn, 3)
        layout.addLayout(action_layout)

        log_header = QHBoxLayout()
        log_header.addWidget(ui_widget(QLabel, tr('日志输出:')))
        log_header.addStretch()
        
        self.clear_btn = ModernButton(tr('清除日志'))
        self.clear_btn.setFixedSize(92, 32)
        self.clear_btn.clicked.connect(self.clear_log)
        log_header.addWidget(self.clear_btn)
        layout.addLayout(log_header)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("LogConsole")
        layout.addWidget(self.log_view)

    def toggle_mode(self, btn):
        is_video = self.radio_video.isChecked()
        self.video_opts_frame.setEnabled(is_video)
        self.btn_ffmpeg.setVisible(is_video)

    def toggle_fps_spin(self, index):
        self.fps_spin.setVisible(index == 1)

    def update_parallel_controls(self):
        enabled = True
        if hasattr(self, "parallel_mode_combo"):
            enabled = self.parallel_mode_combo.currentIndex() != 3
        self.thread_spin.setEnabled(enabled)
        if hasattr(self, "file_parallel_spin"):
            self.file_parallel_spin.setEnabled(True)

    def apply_theme(self):
        theme = self.theme_combo.currentText()
        
        if "System" in theme:
            is_dark = is_windows_dark_mode()
            theme = "Dark" if is_dark else "Light"
        
        if "Dark" in theme:
            self.setStyleSheet("""
                QWidget { font-family: 'Segoe UI', sans-serif; font-size: 10pt; color: #eee; background-color: #2d2d2d; }
                QWidget#MainBackground { background-color: #1e1e1e; }
                QFrame#CardFrame { background-color: #2d2d2d; border-radius: 8px; border: 1px solid #444; }
                QLabel#AppTitle { font-size: 20pt; font-weight: bold; color: #eee; margin-bottom: 10px; }
                QLineEdit { padding: 8px; border: 1px solid #555; border-radius: 4px; background: #3d3d3d; color: #eee; }
                QLineEdit:focus { border-color: #3498db; }
                QTextEdit#LogConsole { background: #111; color: #eee; border-radius: 4px; font-family: Consolas; border: 1px solid #444; }
                QComboBox, QSpinBox { padding: 5px; border: 1px solid #555; border-radius: 4px; background: #3d3d3d; color: #eee; }
                QPushButton#PrimaryButton { background-color: #3498db; color: white; border: none; border-radius: 4px; font-weight: bold; }
                QPushButton#PrimaryButton:hover { background-color: #2980b9; }
                QPushButton#SecondaryButton { background-color: #3d3d3d; border: 1px solid #555; border-radius: 4px; color: #eee; }
                QPushButton#SecondaryButton:hover { background-color: #4d4d4d; }
                QProgressBar { border: 1px solid #555; border-radius: 4px; text-align: center; color: #eee; }
                QProgressBar::chunk { background-color: #2ecc71; }
            """)
        else:
            self.setStyleSheet("""
                QWidget { font-family: 'Segoe UI', sans-serif; font-size: 10pt; color: #333; }
                QWidget#MainBackground { background-color: #f0f2f5; }
                QFrame#CardFrame { background-color: white; border-radius: 8px; border: 1px solid #e0e0e0; }
                QLabel#AppTitle { font-size: 20pt; font-weight: bold; color: #2c3e50; margin-bottom: 10px; }
                QLineEdit { padding: 8px; border: 1px solid #ccc; border-radius: 4px; background: white; }
                QLineEdit:focus { border-color: #3498db; }
                QTextEdit#LogConsole { background: #1e1e1e; color: #eee; border-radius: 4px; font-family: Consolas; }
                QPushButton#PrimaryButton { background-color: #3498db; color: white; border: none; border-radius: 4px; font-weight: bold; }
                QPushButton#PrimaryButton:hover { background-color: #2980b9; }
                QPushButton#SecondaryButton { background-color: white; border: 1px solid #ccc; border-radius: 4px; }
                QPushButton#SecondaryButton:hover { background-color: #f5f5f5; }
                QProgressBar { border: 1px solid #ccc; border-radius: 4px; text-align: center; }
                QProgressBar::chunk { background-color: #2ecc71; }
            """)

    def build_default_output_path(self, input_path: str) -> str:
        raw_path = str(input_path or "").strip().strip('"')
        if not raw_path:
            return ""

        normalized_path = os.path.abspath(raw_path)
        if os.path.isdir(normalized_path):
            base_dir = os.path.dirname(normalized_path)
            folder_name = os.path.basename(os.path.normpath(normalized_path))
            if not folder_name:
                return ""
            return os.path.join(base_dir, f"{folder_name}_out")

        file_dir = os.path.dirname(normalized_path)
        file_name = os.path.basename(normalized_path)
        stem, _ = os.path.splitext(file_name)
        target_name = stem or file_name
        if not target_name:
            return ""
        return os.path.join(file_dir, f"{target_name}_out")

    def sync_output_path_from_input(self, text: str):
        if not hasattr(self, "output_edit"):
            return

        current_output = self.output_edit.text().strip()
        suggested_output = self.build_default_output_path(text)

        if not suggested_output:
            if current_output == self._last_auto_output_path:
                ui_call(self.output_edit, 'clear')
            self._last_auto_output_path = ""
            return

        if not current_output or current_output == self._last_auto_output_path:
            ui_call(self.output_edit, 'setText', suggested_output)
        self._last_auto_output_path = suggested_output

    def browse_input_file(self):
        path, _ = QFileDialog.getOpenFileName(self, tr('选择 BM 文件'), "", "BM Files (*.bm)")
        if path:
            ui_call(self.input_edit, 'setText', path)

    def browse_input_folder(self):
        path = QFileDialog.getExistingDirectory(self, tr('选择包含 BM 文件的文件夹'))
        if path:
            ui_call(self.input_edit, 'setText', path)

    def browse_output_folder(self):
        path = QFileDialog.getExistingDirectory(self, tr('选择输出目录'))
        if path:
            ui_call(self.output_edit, 'setText', path)

    def browse_ffmpeg(self):
        file_path, _ = QFileDialog.getOpenFileName(self, tr('选择 FFmpeg 可执行文件'), "", "Executable Files (*.exe);;All Files (*)")
        if file_path:
            ui_call(self.ffmpeg_edit, 'setText', file_path)
            self.ffmpeg_path = self.ffmpeg_edit.text()
            ui_call(self.btn_ffmpeg, 'setToolTip', tr('当前选择: {0}', self.ffmpeg_path))

    def browse_custom_black_bg_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr('选择 BM 文件'), "", "BM Files (*.bm)")
        if not paths:
            return
        if not hasattr(self, "custom_black_bg_edit"):
            return
        existing = [p.strip() for p in self.custom_black_bg_edit.text().split(";") if p.strip()]
        merged = []
        seen = set()
        for p in existing + paths:
            p = os.path.abspath(p)
            if not p.lower().endswith(".bm"):
                continue
            key = os.path.normcase(p)
            if key in seen:
                continue
            seen.add(key)
            merged.append(p)
        ui_call(self.custom_black_bg_edit, 'setText', ';'.join(merged))

    def _parse_custom_black_bg_files(self):
        if not hasattr(self, "custom_black_bg_edit"):
            return []
        text = self.custom_black_bg_edit.text().strip()
        if not text:
            return []
        parts = text.replace("\n", ";").replace(",", ";").split(";")
        files = []
        seen = set()
        for part in parts:
            p = part.strip().strip('"')
            if not p:
                continue
            p = os.path.abspath(p)
            if not p.lower().endswith(".bm"):
                continue
            key = os.path.normcase(p)
            if key in seen:
                continue
            seen.add(key)
            files.append(p)
        return files

    def log(self, text):
        self.log_view.append(text)

    def clear_log(self):
        ui_call(self.log_view, 'clear')

    def get_active_worker(self):
        if self.worker and self.worker.isRunning():
            return self.worker
        if self.benchmark_worker and self.benchmark_worker.isRunning():
            return self.benchmark_worker
        return None

    def toggle_pause(self):
        worker = self.get_active_worker()
        if not worker:
            return
        self.paused = not self.paused
        worker.set_paused(self.paused)
        ui_call(self.pause_btn, 'setText', tr('继续') if self.paused else tr('暂停'))
        self.log(tr('任务已{0}...', tr('暂停') if self.paused else tr('继续')))

    def stop_conversion(self):
        worker = self.get_active_worker()
        if worker and worker.isRunning():
            reply = QMessageBox.question(self, tr('确认停止'), tr('确定要停止当前任务吗？'), 
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                worker.stop()
                self.log(tr('正在停止任务...'))
                self.stop_btn.setEnabled(False)
                self.pause_btn.setEnabled(False)

    def start_benchmark(self):
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, tr('提示'), tr('正在转换中，无法测速'))
            return
        if self.benchmark_worker and self.benchmark_worker.isRunning():
            return

        input_path = self.input_edit.text().strip()
        if not input_path:
            QMessageBox.warning(self, tr('提示'), tr('请选择输入文件'))
            return

        self.start_btn.setEnabled(False)
        self.benchmark_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        ui_call(self.pause_btn, 'setText', tr('暂停'))
        self.paused = False
        self.progress_bar.setValue(0)
        ui_call(self.progress_bar, 'setFormat', tr('测速中...'))
        ui_call(self.status_label, 'setText', tr('测速中'))
        self.log(tr('开始测速...'))

        self.benchmark_worker = SpeedBenchmarkWorker(input_path)
        self.benchmark_worker.log_signal.connect(self.log)
        self.benchmark_worker.progress_signal.connect(self.update_progress)
        self.benchmark_worker.finished_signal.connect(self.benchmark_finished)
        self.benchmark_worker.start()

    def benchmark_finished(self, success, msg):
        self.start_btn.setEnabled(True)
        self.benchmark_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        ui_call(self.pause_btn, 'setText', tr('暂停'))
        if success:
            self.log(msg)
            ui_call(self.status_label, 'setText', tr('测速完成'))
            ui_call(self.progress_bar, 'setFormat', tr('测速完成'))
            self.progress_bar.setValue(100)
        else:
            if tr('停止').casefold() in msg.casefold():
                ui_call(self.status_label, 'setText', tr('测速已停止'))
                ui_call(self.progress_bar, 'setFormat', tr('测速已停止'))
                self.log(msg)
            else:
                QMessageBox.critical(self, tr('错误'), msg)
                ui_call(self.status_label, 'setText', tr('发生错误'))
                ui_call(self.progress_bar, 'setFormat', tr('发生错误'))
                self.log(msg)

    def start_conversion(self):
        input_path = self.input_edit.text().strip()
        output_path = self.output_edit.text().strip()
        self.ffmpeg_path = self.ffmpeg_edit.text().strip() or "ffmpeg"
        
        if not input_path:
            QMessageBox.warning(self, tr('提示'), tr('请选择输入文件'))
            return
        if not output_path:
            QMessageBox.warning(self, tr('提示'), tr('请选择输出目录'))
            return

        fmt_map = {0: 'webm', 1: 'mov', 2: 'apng', 3: 'mp4', 4: 'mpg'}
        video_fmt = fmt_map[self.format_combo.currentIndex()]
        
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.benchmark_btn.setEnabled(False)
        ui_call(self.pause_btn, 'setText', tr('暂停'))
        self.paused = False
        
        self.progress_bar.setValue(0)
        ui_call(self.progress_bar, 'setFormat', tr('初始化...'))
        ui_call(self.status_label, 'setText', '0/0 (0%)')
        ui_call(self.log_view, 'clear')
        self.log(tr('开始任务...'))

        parallel_mode = self.parallel_mode_combo.currentIndex() if hasattr(self, "parallel_mode_combo") else 0
        auto_mode = parallel_mode == 0
        c_available = _load_decoder_lib() is not None
        if auto_mode:
            enable_multithread = c_available
            enable_multiprocess = not c_available
        else:
            enable_multithread = parallel_mode == 1
            enable_multiprocess = parallel_mode == 2

        thread_count_value = self.thread_spin.value() if hasattr(self, "thread_spin") else 1
        file_process_value = self.file_parallel_spin.value() if hasattr(self, "file_parallel_spin") else 1
        cpu_count = max(1, os.cpu_count() or 1)
        thread_count_effective = _normalize_parallel_count(thread_count_value) if (enable_multithread or enable_multiprocess) else 1

        if file_process_value == 1:
            file_process_count = 1
        elif file_process_value == 0:
            # 提高并发度，允许超过 CPU 核心数，因为任务可能是 IO 密集的
            # 考虑到 Worker 内部会自动限制子线程数（最大4），这里分母取限制后的估算值
            estimated_per_proc = min(thread_count_effective, 4) if thread_count_effective > 1 else 1
            
            # 更加激进的倍率，以响应用户提高并发量的需求
            target_total = max(1, int(cpu_count * 4.0))
            denom = max(1, estimated_per_proc)
            
            file_process_count = max(1, int(target_total / denom))
            
            # 限制上限，避免系统卡死
            file_process_count = min(file_process_count, int(cpu_count * 3))
            
            # 在多核机器上尝试保底为 6
            if cpu_count >= 4:
                file_process_count = max(file_process_count, 6)
        else:
            file_process_count = _normalize_parallel_count(file_process_value)
        enable_file_multiprocess = file_process_count != 1
        force_black_bg_files = self._parse_custom_black_bg_files()

        enable_deblock = bool(getattr(self, "deblock_checkbox", None) and self.deblock_checkbox.isChecked())
        deblock_method = "bilateral"
        if getattr(self, "deblock_method_combo", None) is not None:
            data = self.deblock_method_combo.currentData()
            if isinstance(data, str) and data:
                deblock_method = data
        deblock_strength = 10
        if getattr(self, "deblock_strength_spin", None) is not None:
            deblock_strength = self.deblock_strength_spin.value()

        export_color_mask = bool(getattr(self, "renpy_enable_checkbox", None) and self.renpy_enable_checkbox.isChecked())
        renpy_mode = "side"
        if export_color_mask and getattr(self, "renpy_output_combo", None) is not None:
            data = self.renpy_output_combo.currentData()
            if isinstance(data, str) and data:
                renpy_mode = data
        export_side_mask = export_color_mask and (renpy_mode == "side")

        self.worker = Worker(
            input_path, 
            output_path, 
            self.radio_video.isChecked(),
            video_fmt,
            self.fps_mode.currentIndex() == 1,
            self.fps_spin.value(),
            self.crf_spin.value(),
            self.ffmpeg_path,
            self.remove_black_bg_checkbox.isChecked(),
            self.remove_black_bg_32_checkbox.isChecked(),
            enable_deblock,
            deblock_method,
            deblock_strength,
            enable_multithread,
            enable_multiprocess,
            self.thread_spin.value(),
            export_color_mask,
            export_side_mask,
            bool(self.keep_original_video_checkbox.isChecked() and export_color_mask),
            enable_file_multiprocess,
            file_process_count,
            force_black_bg_files
        )
        self.worker.log_signal.connect(self.log)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.status_signal.connect(self.update_status)
        self.worker.finished_signal.connect(self.conversion_finished)
        self.worker.start()

    def update_status(self, msg: str):
        try:
            ui_call(self.progress_bar, 'setFormat', msg)
        except Exception:
            pass

    def update_progress(self, current, total):
        if total > 0:
            try:
                self.progress_bar.setRange(0, 100)
            except Exception:
                pass
            pct = int(current / total * 100)
            if pct < 0:
                pct = 0
            if pct > 100:
                pct = 100
            self.progress_bar.setValue(pct)
            if self.benchmark_worker and self.benchmark_worker.isRunning():
                ui_call(self.progress_bar, 'setFormat', tr('测速 {0}/{1} ({2}%)', current, total, pct))
            ui_call(self.status_label, 'setText', f'{current}/{total} ({pct}%)')
        else:
            try:
                self.progress_bar.setRange(0, 0)
            except Exception:
                pass
            if self.benchmark_worker and self.benchmark_worker.isRunning():
                ui_call(self.progress_bar, 'setFormat', tr('测速中...'))
            ui_call(self.status_label, 'setText', f'{current}/? (0%)')

    def conversion_finished(self, success, msg):
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.benchmark_btn.setEnabled(True)
        ui_call(self.pause_btn, 'setText', tr('暂停'))
        
        if success:
            QMessageBox.information(self, tr('完成'), msg)
            ui_call(self.progress_bar, 'setFormat', tr('任务完成'))
            self.progress_bar.setValue(100)
            ui_call(self.status_label, 'setText', tr('完成'))
        else:
            if tr('停止').casefold() in msg.casefold():
                ui_call(self.progress_bar, 'setFormat', tr('任务已停止'))
                self.log(tr('任务已强制停止'))
                ui_call(self.status_label, 'setText', tr('停止'))
            else:
                QMessageBox.critical(self, tr('错误'), msg)
                ui_call(self.progress_bar, 'setFormat', tr('发生错误'))
                ui_call(self.status_label, 'setText', tr('错误'))


if __name__ == '__main__':
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
