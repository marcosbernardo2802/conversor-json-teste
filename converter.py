"""
CapCut Draft Converter
Converte o JSON do app de timeline para draft_content.json do CapCut 6.4
"""

import json
import os
import re
import shutil
import tempfile
import uuid
import copy
import threading
import subprocess
import platform
from pathlib import Path
from datetime import datetime
import wave


# ─── Config persistente ──────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "converter_config.json"
TEMPLATES_PATH = Path(__file__).parent / "capcut_templates.json"

def load_templates() -> dict:
    """Carrega os moldes de efeitos (zoom, transição, VHS, marca do canal) e adapta caminhos."""
    try:
        content = TEMPLATES_PATH.read_text(encoding="utf-8")
        if platform.system() == "Windows":
            userprofile = os.environ.get("USERPROFILE", "").replace("\\", "/")
            if userprofile:
                import re
                content = re.sub(r'[A-Za-z]:/Users/[^/]+/AppData/Local', f"{userprofile}/AppData/Local", content)
        return json.loads(content)
    except Exception:
        return {}

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ─── Utilitários ─────────────────────────────────────────────────────────────

def sec_to_us(seconds: float) -> int:
    return max(0, int(round(seconds * 1_000_000)))

def fix_path(folder: str, filename: str) -> str:
    p = Path(folder) / filename
    return str(p).replace("\\", "/")

def new_uuid() -> str:
    return str(uuid.uuid4()).upper()

def get_audio_duration_us(path: str):
    """
    Retorna a duração em microssegundos de arquivos de áudio (WAV, MP3, etc.).
    Para WAV, utiliza o módulo nativo wave (rápido e exato).
    Para MP3 e outros formatos, utiliza o FFmpeg para precisão milimétrica absoluta (evitando drift).
    Possui fallback interno caso o FFmpeg não esteja disponível.
    """
    if not path or not os.path.isfile(path):
        return None

    # 1. WAV via módulo nativo wave (rápido e 100% exato)
    try:
        with wave.open(path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate > 0:
                return sec_to_us(frames / float(rate))
    except Exception:
        pass

    # 2. WAV via leitura direta de chunks RIFF/WAVE
    import struct
    try:
        with open(path, "rb") as f:
            header = f.read(12)
        if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
            with open(path, "rb") as f:
                f.read(12)
                block_align = sample_rate = 0
                while True:
                    chunk = f.read(8)
                    if len(chunk) < 8:
                        break
                    tag, size = chunk[:4], struct.unpack_from("<I", chunk, 4)[0]
                    if tag == b"fmt ":
                        fmt_bytes_to_read = min(size, 16)
                        fmt = f.read(fmt_bytes_to_read)
                        _, sample_rate = struct.unpack_from("<HI", fmt, 2)
                        block_align = struct.unpack_from("<H", fmt, 12)[0]
                        if size > fmt_bytes_to_read:
                            f.seek(size - fmt_bytes_to_read, 1)
                    elif tag == b"data":
                        num_samples = size // block_align if block_align else 0
                        return sec_to_us(num_samples / sample_rate) if sample_rate else None
                    else:
                        f.seek(size, 1)
            return None
    except Exception:
        pass

    # 3. Leitura precisa via FFmpeg (MP3, AAC, M4A, OGG, FLAC, etc.)
    try:
        ffmpeg = get_ffmpeg_bin()
        if ffmpeg:
            creationflags = 0x08000000 if platform.system() == "Windows" else 0
            res = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, creationflags=creationflags)
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr)
            if m:
                hours = int(m.group(1))
                mins = int(m.group(2))
                secs = float(m.group(3))
                total_secs = hours * 3600 + mins * 60 + secs
                return int(round(total_secs * 1_000_000))
    except Exception:
        pass

    # 4. Fallback manual para MP3 quando FFmpeg não estiver disponível
    try:
        with open(path, "rb") as f:
            raw = f.read(10)
            offset = 0
            if raw[:3] == b"ID3":
                sz = ((raw[6] & 0x7F) << 21 | (raw[7] & 0x7F) << 14 |
                      (raw[8] & 0x7F) << 7  | (raw[9] & 0x7F))
                offset = 10 + sz
            f.seek(offset)
            data = f.read(min(256 * 1024, os.path.getsize(path) - offset))

        BITRATES    = [0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0]
        SAMPLERATES = [44100, 48000, 32000, 0]
        total_secs = 0.0
        frames = 0
        i = 0
        while i < len(data) - 4:
            b0, b1, b2 = data[i], data[i+1], data[i+2]
            if b0 == 0xFF and (b1 & 0xE0) == 0xE0:
                br_idx = (b2 >> 4) & 0xF
                sr_idx = (b2 >> 2) & 0x3
                padding  = (b2 >> 1) & 0x1
                bitrate  = BITRATES[br_idx] * 1000
                samplerate = SAMPLERATES[sr_idx]
                if bitrate and samplerate:
                    frame_size  = 144 * bitrate // samplerate + padding
                    total_secs += 1152 / samplerate
                    frames += 1
                    if frames >= 200:
                        file_size  = os.path.getsize(path) - offset
                        est_frames = file_size / frame_size if frame_size else frames
                        return sec_to_us(total_secs / frames * est_frames)
                    i += max(frame_size, 1)
                    continue
            i += 1
        return sec_to_us(total_secs) if total_secs > 0 else None
    except Exception:
        return None

def set_drafts_dir(path: str) -> dict:
    """Valida e grava o caminho da pasta de drafts do CapCut no converter_config.json."""
    if not path or not os.path.isdir(path):
        return {"ok": False, "error": f"Pasta de drafts não encontrada ou inválida: {path}"}
    try:
        cfg = load_config()
        cfg["drafts_dir"] = str(Path(path).resolve())
        save_config(cfg)
        return {"ok": True, "drafts_dir": cfg["drafts_dir"]}
    except Exception as e:
        return {"ok": False, "error": f"Erro ao salvar configuração: {e}"}


def find_capcut_drafts() -> str:
    """Detecta a pasta de drafts do CapCut no sistema ou recupera das configurações."""
    cfg = load_config()
    saved = (cfg.get("drafts_dir") or "").strip()
    if saved and os.path.isdir(saved):
        return saved

    system = platform.system()
    candidates = []
    if system == "Windows":
        base = os.environ.get("USERPROFILE", "C:/Users/User")
        candidates = [
            Path(base) / "AppData" / "Local" / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft",
            Path(base) / "AppData" / "Local" / "Cap Cut" / "User Data" / "Projects" / "com.lveditor.draft",
        ]
        # Busca em AppData/Local por qualquer pasta CapCut
        local = Path(base) / "AppData" / "Local"
        if local.exists():
            try:
                for d in local.iterdir():
                    if "capcut" in d.name.lower() or "lveditor" in d.name.lower():
                        draft_path = d / "User Data" / "Projects" / "com.lveditor.draft"
                        candidates.append(draft_path)
            except Exception:
                pass
    elif system == "Darwin":
        candidates = [
            Path.home() / "Movies" / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft",
        ]
    else:
        candidates = [
            Path.home() / ".local" / "share" / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft",
        ]
    for c in candidates:
        if c.exists():
            return str(c)
    # Retorna o padrão mesmo que não exista
    return str(candidates[0]) if candidates else ""

def get_media_dimensions(file_path: str) -> tuple[int, int]:
    """
    Retorna a largura e altura (width, height) reais do arquivo de mídia.
    Suporta imagens (PNG, JPG, WEBP, GIF, BMP, etc.) e vídeos (MP4, MOV, etc.).
    """
    if not file_path or not os.path.isfile(file_path):
        return 0, 0

    # 1. Tenta Pillow (rápido e robusto para imagens)
    try:
        from PIL import Image
        with Image.open(file_path) as img:
            w, h = img.size
            if w > 0 and h > 0:
                return int(w), int(h)
    except Exception:
        pass

    # 2. Leitura rápida de cabeçalhos binários nativos
    try:
        with open(file_path, "rb") as f:
            head = f.read(32)
            if head.startswith(b"\x89PNG\r\n\x1a\n"):
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h)
            if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
                w, h = struct.unpack("<HH", head[6:10])
                return int(w), int(h)
            if head.startswith(b"BM"):
                w, h = struct.unpack("<II", head[18:26])
                return int(w), int(h)
    except Exception:
        pass

    # 3. Tenta via FFmpeg para vídeos (MP4, MOV, MKV, etc.) e outras mídias
    try:
        ffmpeg = get_ffmpeg_bin()
        if ffmpeg:
            creationflags = 0x08000000 if platform.system() == "Windows" else 0
            res = subprocess.run([ffmpeg, "-hide_banner", "-i", str(file_path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 creationflags=creationflags)
            m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", res.stderr)
            if m:
                return int(m.group(1)), int(m.group(2))
    except Exception:
        pass

    return 0, 0


def compute_fit_scale(fit: str, img_w: int, img_h: int, canvas_w: int, canvas_h: int):
    """
    Calcula scale e translation para contain/cover/fill.
    Retorna (scale_x, scale_y, trans_x, trans_y).
    """
    if img_w <= 0 or img_h <= 0:
        return 1.0, 1.0, 0.0, 0.0
    img_ratio    = img_w / img_h
    canvas_ratio = canvas_w / canvas_h
    if fit == "cover":
        if img_ratio > canvas_ratio:
            sy = 1.0
            sx = (canvas_h * img_ratio) / canvas_w
        else:
            sx = 1.0
            sy = (canvas_w / img_ratio) / canvas_h
        return sx, sy, 0.0, 0.0
    elif fit == "contain":
        if img_ratio > canvas_ratio:
            sx = 1.0
            sy = canvas_w / (img_ratio * canvas_h)
        else:
            sy = 1.0
            sx = (img_ratio * canvas_h) / canvas_w
        return sx, sy, 0.0, 0.0
    else:  # fill / stretch
        return 1.0, 1.0, 0.0, 0.0


def make_scene_animation(templates: dict, dur_us: int, zoom_threshold_us: int, anim_type: str = None) -> dict:
    """Cria o material 'sticker_animation' (Zoom 1 ou Quicar 1) para uma cena,
    já com a duração ajustada ao tamanho do clipe."""
    if anim_type:
        tpl = templates.get(anim_type, {})
    else:
        tpl = templates.get("quicar_animation" if dur_us > zoom_threshold_us
                             else "zoom_animation", {})
    anim_id = new_uuid()
    return anim_id, {
        "animations": [{
            "anim_adjust_params": None,
            "category_id": tpl.get("category_id", "7039"),
            "category_name": tpl.get("category_name", "Combinação"),
            "duration": dur_us,
            "id": tpl.get("resource_id", ""),
            "material_type": "video",
            "name": tpl.get("name", ""),
            "panel": "video",
            "path": "",
            "platform": "all",
            "request_id": "",
            "resource_id": tpl.get("resource_id", ""),
            "source_platform": 1,
            "start": 0,
            "third_resource_id": tpl.get("resource_id", ""),
            "type": "group",
        }],
        "id": anim_id,
        "multi_language_current": "none",
        "type": "sticker_animation",
    }


def make_scene_transition(templates: dict, duration_us: int) -> dict:
    """Cria o material de transição 'Esmaecimento preto' entre duas cenas."""
    tpl = templates.get("transition", {})
    trans_id = new_uuid()
    return trans_id, {
        "category_id": tpl.get("category_id", "100000"),
        "category_name": "",
        "duration": duration_us,
        "effect_id": tpl.get("resource_id", ""),
        "id": trans_id,
        "is_overlap": False,
        "name": tpl.get("name", "Esmaecimento preto"),
        "path": "",
        "platform": "all",
        "request_id": "",
        "resource_id": tpl.get("resource_id", ""),
        "source_platform": 1,
        "third_resource_id": tpl.get("resource_id", ""),
        "type": "transition",
    }


def build_vhs_layer(templates: dict, total_us: int):
    """Monta a camada de filtro 'Cintilação retrô' (efeito VHS), cobrindo o vídeo inteiro."""
    tpl = templates.get("vhs_video_effect_template")
    if not tpl:
        return None, None
    mat = copy.deepcopy(tpl)
    mat["id"] = new_uuid()
    mat["apply_time_range"] = None
    mat["time_range"] = None
    track = {
        "attribute": 0, "flag": 0, "id": new_uuid(),
        "is_default_name": True, "name": "", "type": "effect",
        "segments": [{
            "caption_info": None, "cartoon": False, "clip": None,
            "color_correct_alg_result": "", "common_keyframes": [],
            "desc": "", "digital_human_template_group_id": "",
            "enable_adjust": False, "enable_adjust_mask": False,
            "enable_color_correct_adjust": False, "enable_color_curves": True,
            "enable_color_match_adjust": False, "enable_color_wheels": True,
            "enable_hsl": False, "enable_lut": False,
            "enable_smart_color_adjust": False, "enable_video_mask": True,
            "extra_material_refs": [], "group_id": "",
            "hdr_settings": None, "id": new_uuid(),
            "intensifies_audio": False, "is_loop": False, "is_placeholder": False,
            "is_tone_modify": False, "keyframe_refs": [],
            "last_nonzero_volume": 1.0, "lyric_keyframes": None,
            "material_id": mat["id"], "raw_segment_id": "",
            "render_index": 0, "render_timerange": {"duration": 0, "start": 0},
            "responsive_layout": {"enable": False, "horizontal_pos_layout": 0,
                                  "size_layout": 0, "target_follow": "",
                                  "vertical_pos_layout": 0},
            "reverse": False, "source": "segmentsourcenormal",
            "source_timerange": {"duration": total_us, "start": 0},
            "speed": 1.0, "state": 0,
            "target_timerange": {"start": 0, "duration": total_us},
            "template_id": "", "template_scene": "default",
            "track_attribute": 0, "track_render_index": 0,
            "uniform_scale": None, "visible": True, "volume": 1.0,
        }],
    }
    return track, mat


def build_watermark_layer(templates: dict, total_us: int, watermark_text: str):
    """Monta a camada de texto (marca do canal), cobrindo o vídeo inteiro."""
    wm = templates.get("watermark")
    if not wm:
        return None, None, None, None, None

    text_mat = copy.deepcopy(wm["text_material_template"])
    text_mat["id"] = new_uuid()
    if watermark_text:
        try:
            content = json.loads(text_mat.get("content", "{}"))
            content["text"] = watermark_text
            if content.get("styles"):
                content["styles"][0]["range"] = [0, len(watermark_text)]
            text_mat["content"] = json.dumps(content, ensure_ascii=False)
        except Exception:
            pass

    template_mat = copy.deepcopy(wm["text_template_material_template"])
    template_mat["id"] = new_uuid()
    for res in template_mat.get("text_info_resources", []):
        res["text_material_id"] = text_mat["id"]

    text_effect_mat = copy.deepcopy(wm["text_effect_material_template"])
    text_effect_mat["id"] = new_uuid()

    decorative_anims = []
    anim_ids = []
    for anim_tpl in wm.get("decorative_animations_templates", []):
        anim = copy.deepcopy(anim_tpl)
        anim["id"] = new_uuid()
        decorative_anims.append(anim)
        anim_ids.append(anim["id"])

    # ordem original: anim0, text_effect, anim1, anim2
    extra_refs = []
    if len(anim_ids) > 0:
        extra_refs.append(anim_ids[0])
    extra_refs.append(text_effect_mat["id"])
    extra_refs.extend(anim_ids[1:])

    clip_cfg = wm.get("segment_clip", {
        "alpha": 1.0, "flip": {"horizontal": False, "vertical": False},
        "rotation": 0.0, "scale": {"x": 0.11, "y": 0.11},
        "transform": {"x": -0.8167576413673124, "y": 0.9549669091028111},
    })

    track = {
        "attribute": 0, "flag": 0, "id": new_uuid(),
        "is_default_name": True, "name": "", "type": "text",
        "segments": [{
            "caption_info": None, "cartoon": False,
            "clip": copy.deepcopy(clip_cfg),
            "color_correct_alg_result": "", "common_keyframes": [],
            "desc": "", "digital_human_template_group_id": "",
            "enable_adjust": False, "enable_adjust_mask": False,
            "enable_color_correct_adjust": False, "enable_color_curves": True,
            "enable_color_match_adjust": False, "enable_color_wheels": True,
            "enable_hsl": False, "enable_lut": False,
            "enable_smart_color_adjust": False, "enable_video_mask": True,
            "extra_material_refs": extra_refs, "group_id": "",
            "hdr_settings": None, "id": new_uuid(),
            "intensifies_audio": False, "is_loop": False, "is_placeholder": False,
            "is_tone_modify": False, "keyframe_refs": [],
            "last_nonzero_volume": 1.0, "lyric_keyframes": None,
            "material_id": template_mat["id"], "raw_segment_id": "",
            "render_index": 0, "render_timerange": {"duration": 0, "start": 0},
            "responsive_layout": {"enable": False, "horizontal_pos_layout": 0,
                                  "size_layout": 0, "target_follow": "",
                                  "vertical_pos_layout": 0},
            "reverse": False, "source": "segmentsourcenormal",
            "source_timerange": {"duration": total_us, "start": 0},
            "speed": 1.0, "state": 0,
            "target_timerange": {"start": 0, "duration": total_us},
            "template_id": "", "template_scene": "default",
            "track_attribute": 0, "track_render_index": 0,
            "uniform_scale": None, "visible": True, "volume": 1.0,
        }],
    }
    return track, text_mat, template_mat, text_effect_mat, decorative_anims


# ─── Conversor ───────────────────────────────────────────────────────────────

def find_companion_image(video_name, media_items, media_folder):
    """
    Busca uma imagem companheira com o mesmo nome base do vídeo (ex: '0003.jpg' para '0003.mp4').
    Procura primeiro em media_items, depois diretamente na media_folder.
    """
    if not video_name:
        return None
    stem = Path(video_name).stem.lower().strip()
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

    # 1. Procura em media_items
    for item in (media_items or []):
        iname = item.get("name", "")
        if not iname:
            continue
        ipath = Path(iname)
        if ipath.suffix.lower() in image_exts and ipath.stem.lower().strip() == stem:
            return dict(item)

    # 2. Procura diretamente na media_folder
    if media_folder and os.path.isdir(media_folder):
        try:
            for fname in os.listdir(media_folder):
                fpath = Path(fname)
                if fpath.suffix.lower() in image_exts and fpath.stem.lower().strip() == stem:
                    full_p = os.path.join(media_folder, fname)
                    rw, rh = get_media_dimensions(full_p)
                    return {
                        "id": str(uuid.uuid4()).lower(),
                        "name": fname,
                        "type": "image/jpeg",
                        "width": rw,
                        "height": rh,
                        "duration": 5.0,
                    }
        except Exception:
            pass

    return None


def extract_video_freeze_frame(video_path, output_dir, base_name):
    """
    Extrai o último frame do vídeo como imagem congelada (freeze frame) usando FFmpeg.
    Retorna o dict da mídia gerada ou None se falhar.
    """
    ffmpeg_bin = get_ffmpeg_bin()
    if not ffmpeg_bin or not os.path.isfile(video_path):
        return None

    try:
        os.makedirs(output_dir, exist_ok=True)
        out_name = f"{base_name}_freeze.jpg"
        out_path = os.path.join(output_dir, out_name)

        cmd = [
            ffmpeg_bin, "-y",
            "-sseof", "-0.1",
            "-i", video_path,
            "-frames:v", "1",
            "-update", "1",
            out_path
        ]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=10
        )
        if proc.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            rw, rh = get_media_dimensions(out_path)
            return {
                "id": str(uuid.uuid4()).lower(),
                "name": out_name,
                "type": "image/jpeg",
                "width": rw,
                "height": rh,
                "duration": 5.0,
                "_is_freeze": True,
            }
    except Exception:
        pass
    return None


def create_segment_dict(seg_id, material_id, start_us, target_dur_us,
                        source_start_us, source_dur_us, speed_val,
                        scale_x, scale_y, trans_x, trans_y, extra_refs, volume=1.0):
    return {
        "caption_info": None, "cartoon": False,
        "clip": {"alpha": 1.0,
                 "flip": {"horizontal": False, "vertical": False},
                 "rotation": 0.0,
                 "scale": {"x": scale_x, "y": scale_y},
                 "translation": {"x": trans_x, "y": trans_y}},
        "color_correct_alg_result": "",
        "common_keyframes": [],
        "desc": "",
        "digital_human_template_group_id": "",
        "enable_adjust": True, "enable_adjust_mask": False,
        "enable_color_correct_adjust": False,
        "enable_color_curves": True, "enable_color_match_adjust": False,
        "enable_color_wheels": True, "enable_hsl": False,
        "enable_lut": True, "enable_smart_color_adjust": False,
        "enable_video_mask": True,
        "extra_material_refs": extra_refs,
        "group_id": "",
        "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000},
        "id": seg_id,
        "intensifies_audio": False,
        "is_loop": False, "is_placeholder": False, "is_tone_modify": False,
        "keyframe_refs": [],
        "last_nonzero_volume": 1.0,
        "lyric_keyframes": None,
        "material_id": material_id,
        "raw_segment_id": "",
        "render_index": 0,
        "render_timerange": {"duration": 0, "start": 0},
        "responsive_layout": {"enable": False, "horizontal_pos_layout": 0,
                              "size_layout": 0, "target_follow": "",
                              "vertical_pos_layout": 0},
        "reverse": False,
        "source": "segmentsourcenormal",
        "source_timerange": {"duration": source_dur_us, "start": source_start_us},
        "speed": speed_val, "state": 0,
        "target_timerange": {"start": start_us, "duration": target_dur_us},
        "template_id": "", "template_scene": "default",
        "track_attribute": 0, "track_render_index": 0,
        "uniform_scale": {"on": True, "value": 1.0},
        "visible": True,
        "volume": volume,
    }

def build_draft(project_name, clips, media_items, media_folder,
                audio_files, resolution, fps, img_ext="jpg",
                progress_cb=None,
                apply_zoom=True, apply_transition=True,
                apply_vhs=True, apply_watermark=True,
                zoom_threshold=5.0, transition_duration=0.4667,
                watermark_text="", allow_missing=False):
    """
    audio_files: lista de dicts [{"name": "narr.mp3", "volume": 1.0}, ...]
    progress_cb: callable(current, total) para atualizar barra de progresso

    apply_zoom / apply_transition / apply_vhs / apply_watermark:
        liga/desliga cada efeito opcional.
    zoom_threshold: duração (segundos) que decide entre "Zoom 1" (<=) e "Quicar 1" (>).
    transition_duration: duração (segundos) da transição "Esmaecimento preto".
    watermark_text: texto exibido na marca do canal (se apply_watermark=True).
    """
    templates = load_templates()
    res_map = {
        "1920x1080": (1920, 1080),
        "1080x1920": (1080, 1920),
        "1080x1080": (1080, 1080),
        "720x1280":  (720,  1280),
    }
    width, height = res_map.get(resolution, (1920, 1080))

    valid_clips = [c for c in clips if c.get("duration", 0) > 0]

    IMAGE_TYPES_L = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
    IMAGE_EXTS_L  = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
    VIDEO_EXTS_L  = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
    AUDIO_EXTS_L  = {".mp3", ".wav", ".aac", ".m4a", ".ogg", ".flac", ".wma"}

    # Mapa preliminar de mídias para identificar tipos
    raw_media_map = {m["id"]: m for m in (media_items or []) if isinstance(m, dict) and "id" in m}

    # 1. Separação de clips de vídeo/imagem vs clips de áudio
    video_clips_in = []
    json_audio_clips = []

    for c in valid_clips:
        track_id = str(c.get("trackId", "")).lower()
        mid = c.get("mediaId", "")
        m_info = raw_media_map.get(mid, {})
        m_type = str(m_info.get("type", "")).lower()
        m_name = str(m_info.get("name", "")).lower()
        m_ext  = Path(m_name).suffix

        is_audio = (
            track_id.startswith("audio")
            or m_type.startswith("audio/")
            or m_ext in AUDIO_EXTS_L
        )
        if is_audio:
            json_audio_clips.append(c)
        else:
            video_clips_in.append(c)

    # 2. Normalização de mídias (evita alterar extensões de vídeos ou áudios)
    normalized_media = []
    if not media_items:
        seen = {}
        seq = 1
        for c in video_clips_in:
            mid = c.get("mediaId", "")
            if mid and mid not in seen:
                seen[mid] = {
                    "id": mid,
                    "name": f"{seq:04d}.{img_ext}",
                    "type": f"image/{img_ext}",
                    "duration": c.get("duration", 3.0),
                }
                seq += 1
        normalized_media = list(seen.values())
    else:
        for m in media_items:
            m_copy = dict(m)
            name = m_copy.get("name", "")
            ext = Path(name).suffix.lower()
            item_type = str(m_copy.get("type", "")).lower()

            is_video = ext in VIDEO_EXTS_L or item_type.startswith("video/")
            is_audio = ext in AUDIO_EXTS_L or item_type.startswith("audio/")
            is_image = item_type in IMAGE_TYPES_L or ext in IMAGE_EXTS_L or (not is_video and not is_audio)

            if is_image and img_ext and not is_video and not is_audio:
                stem = Path(name).stem if name else m_copy.get("id", "media")
                m_copy["name"] = f"{stem}.{img_ext}"

            m_copy["_is_video"] = is_video
            m_copy["_is_audio"] = is_audio
            normalized_media.append(m_copy)

    media_map = {m["id"]: m for m in normalized_media}
    missing_scenes = []
    resolved_clips = []

    for idx, c in enumerate(video_clips_in, start=1):
        mid = c.get("mediaId", "")
        if mid in media_map:
            resolved_clips.append((c, media_map[mid]))
        else:
            missing_scenes.append(f"Cena {idx} (duração {c.get('duration', 0)}s, ID de mídia '{mid}')")
            if allow_missing:
                placeholder_m = {
                    "id": mid or f"missing_{idx}",
                    "name": f"placeholder_cena_{idx}.{img_ext}",
                    "type": f"image/{img_ext}",
                    "duration": c.get("duration", 3.0),
                    "_is_video": False,
                    "_is_audio": False,
                    "_placeholder": True,
                }
                resolved_clips.append((c, placeholder_m))

    if missing_scenes and not allow_missing:
        detail = "\n".join(f"  • {m}" for m in missing_scenes)
        raise ValueError(
            f"Erro na conversão: foram detectadas mídias ausentes em {len(missing_scenes)} cena(s).\n"
            f"Remover essas cenas encurtaria o vídeo e causaria dessincronização com o áudio.\n\n"
            f"Cenas afetadas:\n{detail}"
        )

    # Calcula os tempos de início (start) respeitando o startTime do projeto ou acumulando gaps
    clip_start_times = {}
    current_time = 0.0
    max_video_end = 0.0

    for c, _ in resolved_clips:
        dur = float(c.get("duration", 0.0))
        if c.get("startTime") is not None:
            c_start = float(c["startTime"])
            c_start = max(0.0, c_start)
        else:
            c_start = current_time

        clip_start_times[id(c)] = c_start
        current_time = c_start + dur
        if current_time > max_video_end:
            max_video_end = current_time

    total_us = sec_to_us(max_video_end)
    now = int(datetime.now().timestamp())

    # ── Consolidação de áudios (Interface + JSON) ───────────────────────────
    combined_audio_files = list(audio_files or [])
    existing_audio_names = {str(a.get("name", "")).lower() for a in combined_audio_files if "name" in a}

    for ac in json_audio_clips:
        mid = ac.get("mediaId", "")
        m_info = media_map.get(mid, raw_media_map.get(mid, {}))
        aname = m_info.get("name", "")
        if aname and aname.lower() not in existing_audio_names:
            combined_audio_files.append({
                "name": aname,
                "volume": float(ac.get("volume", 1.0)),
                "start_time": float(ac.get("startTime", 0.0)),
                "duration": float(ac.get("duration", 0.0)),
            })
            existing_audio_names.add(aname.lower())

    total_steps = len(resolved_clips) + len(combined_audio_files)

    speeds = []
    sound_channels = []
    vocal_seps = []
    video_materials = []
    registered_materials = {}  # id -> mat_dict
    material_animations = []   # zoom/quicar por cena (apenas fotos)
    transitions_list = []      # transições entre cenas
    audio_fades_list = []      # fades de áudio por cena (fadeIn/fadeOut)
    video_segments = []

    zoom_threshold_us = sec_to_us(zoom_threshold)
    transition_dur_us = sec_to_us(transition_duration)

    def register_material(m_info, is_vid, f_path, dur_micros):
        mat_id = m_info.get("id") or str(uuid.uuid4()).lower()
        if mat_id in registered_materials:
            return mat_id, registered_materials[mat_id]

        img_w = int(m_info.get("width") or 0)
        img_h = int(m_info.get("height") or 0)
        if (img_w <= 0 or img_h <= 0) and media_folder and os.path.isfile(f_path):
            rw, rh = get_media_dimensions(f_path)
            if rw > 0 and rh > 0:
                img_w, img_h = rw, rh
                m_info["width"] = rw
                m_info["height"] = rh

        mat_w = int(img_w or width)
        mat_h = int(img_h or height)
        local_mat_id = str(uuid.uuid4()).lower()

        mat_dict = {
            "aigc_history_id": "", "aigc_item_id": "", "aigc_type": "none",
            "audio_fade": None,
            "beauty_body_preset_id": "",
            "beauty_face_auto_preset": {"name": "", "preset_id": "", "rate_map": ""},
            "beauty_face_auto_preset_infos": [], "beauty_face_preset_infos": [],
            "cartoon_path": "", "category_id": "",
            "category_name": "local",
            "check_flag": 62978047,
            "crop": {"lower_left_x": 0.0, "lower_left_y": 1.0,
                     "lower_right_x": 1.0, "lower_right_y": 1.0,
                     "upper_left_x": 0.0, "upper_left_y": 0.0,
                     "upper_right_x": 1.0, "upper_right_y": 0.0},
            "crop_ratio": "free", "crop_scale": 1.0,
            "duration": dur_micros,
            "extra_type_option": 0 if is_vid else 2,
            "formula_id": "", "freeze": None,
            "has_audio": is_vid, "has_sound_separated": False,
            "height": mat_h, "id": mat_id,
            "intensifies_audio_path": "", "intensifies_path": "",
            "is_ai_generate_content": False, "is_copyright": False,
            "is_text_edit_overdub": False, "is_unified_beauty_mode": False,
            "live_photo_cover_path": "", "live_photo_timestamp": -1,
            "local_id": "", "local_material_from": "",
            "local_material_id": local_mat_id,
            "material_id": "", "material_name": m_info.get("name", ""), "material_url": "",
            "matting": {"custom_matting_id": "", "enable_matting_stroke": False,
                        "expansion": 0, "feather": 0, "flag": 0,
                        "has_use_quick_brush": False, "has_use_quick_eraser": False,
                        "interactiveTime": [], "path": "", "strokes": []},
            "media_path": "", "multi_camera_info": None, "object_locked": None,
            "origin_material_id": "",
            "path": f_path,
            "picture_from": "none", "picture_set_category_id": "",
            "picture_set_category_name": "",
            "request_id": "", "reverse_intensifies_path": "", "reverse_path": "",
            "smart_match_info": None, "smart_motion": None,
            "source": 0, "source_platform": 0,
            "stable": {"matrix_path": "", "stable_level": 0,
                       "time_range": {"duration": 0, "start": 0}},
            "team_id": "",
            "type": "video" if is_vid else "photo",
            "video_algorithm": {
                "ai_background_configs": [], "ai_expression_driven": None,
                "ai_motion_driven": None, "algorithms": [],
                "complement_frame_config": None, "deflicker": None,
                "gameplay_configs": [], "motion_blur_config": None,
                "noise_reduction": None, "path": "",
                "quality_enhance_config": None, "time_range": None,
            },
            "width": mat_w,
        }
        video_materials.append(mat_dict)
        registered_materials[mat_id] = mat_dict
        return mat_id, mat_dict

    def make_speed_and_audio(speed_v=1.0):
        sp_id = new_uuid()
        speeds.append({"curve_speed": None, "id": sp_id, "mode": 0,
                       "speed": speed_v, "type": "speed"})
        sc_id = new_uuid()
        sound_channels.append({"audio_channel_mapping": 0, "id": sc_id,
                               "is_config_open": False, "type": "none"})
        vs_id = new_uuid()
        vocal_seps.append({"choice": 0, "id": vs_id, "production_path": "",
                           "removed_sounds": [], "time_range": None,
                           "type": "vocal_separation"})
        return [sp_id, sc_id, vs_id]

    num_scenes = len(resolved_clips)
    for scene_idx, (clip, m) in enumerate(resolved_clips):
        if progress_cb:
            progress_cb(scene_idx, total_steps)

        is_last_scene = (scene_idx == num_scenes - 1)
        dur_s = float(clip.get("duration", 0.0))
        clip_start_s = clip_start_times[id(clip)]
        start_us = sec_to_us(clip_start_s)
        dur_us = sec_to_us(dur_s)
        is_video = m.get("_is_video", False) or Path(m["name"]).suffix.lower() in VIDEO_EXTS_L
        orig_dur_s = float(m.get("duration") or dur_s)
        offset_s = float(clip.get("offset", 0.0))
        file_path = fix_path(media_folder, m["name"])
        vol = float(clip.get("volume", 1.0))

        # Regras de velocidade / desaceleração / vídeo vs imagem companheira:
        split_to_companion = False
        companion_m = None

        if clip.get("speed"):
            # Velocidade explicitada pelo projeto
            speed_val = float(clip["speed"])
            source_dur_s = dur_s * speed_val
            source_start_s = offset_s
        elif not is_video:
            # Imagem estática normal
            speed_val = 1.0
            source_dur_s = dur_s
            source_start_s = offset_s
        elif orig_dur_s <= 0 or dur_s <= orig_dur_s:
            # Caso A: Cena <= vídeo original (corta vídeo na velocidade normal 1.0x)
            speed_val = 1.0
            source_dur_s = dur_s
            source_start_s = offset_s
        elif dur_s <= (orig_dur_s * 1.40):
            # Caso B: Cena até 40% maior que o vídeo (desaceleração suave controlada)
            speed_val = round(orig_dur_s / dur_s, 4)
            source_dur_s = orig_dur_s
            source_start_s = 0.0
        else:
            # Caso C: Cena > 140% do vídeo original
            # Busca imagem companheira nos media_items ou pasta
            companion_m = find_companion_image(m["name"], media_items, media_folder)
            if not companion_m and media_folder:
                freeze_dir = media_folder or tempfile.gettempdir()
                companion_m = extract_video_freeze_frame(file_path, freeze_dir, Path(m["name"]).stem)

            if companion_m:
                split_to_companion = True
            else:
                # Fallback sem imagem companheira nem freeze frame gerado
                speed_val = round(orig_dur_s / dur_s, 4)
                source_dur_s = orig_dur_s
                source_start_s = 0.0

        if split_to_companion and companion_m:
            # ── Subcaso C1: Vídeo (1.0x) + Imagem Companheira preenchendo o restante ──
            v_mid, v_mat = register_material(m, True, file_path, sec_to_us(orig_dur_s))
            comp_file_path = fix_path(media_folder, companion_m["name"])
            c_mid, c_mat = register_material(companion_m, False, comp_file_path, sec_to_us(dur_s - orig_dur_s))

            # Segmento 1: Vídeo
            v_dur_s = orig_dur_s
            v_dur_us = sec_to_us(v_dur_s)
            v_refs = make_speed_and_audio(1.0)

            fin_s = float(clip.get("fadeIn") or 0.0)
            fout_s = float(clip.get("fadeOut") or 0.0)
            if fin_s > 0 or fout_s > 0:
                fade_id = new_uuid()
                audio_fades_list.append({
                    "fade_in_duration": sec_to_us(fin_s),
                    "fade_out_duration": sec_to_us(fout_s),
                    "id": fade_id,
                    "type": "audio_fade"
                })
                v_refs.append(fade_id)

            v_fit = clip.get("fit", m.get("fit", "contain"))
            v_sx, v_sy, v_tx, v_ty = compute_fit_scale(v_fit, v_mat["width"], v_mat["height"], width, height)

            video_segments.append(create_segment_dict(
                seg_id=new_uuid(),
                material_id=v_mid,
                start_us=start_us,
                target_dur_us=v_dur_us,
                source_start_us=0,
                source_dur_us=v_dur_us,
                speed_val=1.0,
                scale_x=v_sx, scale_y=v_sy,
                trans_x=v_tx, trans_y=v_ty,
                extra_refs=v_refs,
                volume=vol
            ))

            # Segmento 2: Imagem companheira
            img_dur_s = dur_s - orig_dur_s
            img_dur_us = sec_to_us(img_dur_s)
            img_start_us = start_us + v_dur_us
            img_refs = make_speed_and_audio(1.0)

            # Zoom / Animação na imagem companheira
            c_anim = clip.get("animation", clip.get("zoom"))
            forced_anim = None
            if c_anim is not None and str(c_anim).lower() in ("quicar", "bounce"):
                apply_this_zoom = True
                forced_anim = "quicar_animation"
            elif c_anim is not None and str(c_anim).lower() in ("zoom", "zoom1"):
                apply_this_zoom = True
                forced_anim = "zoom_animation"
            elif c_anim is not None and str(c_anim).lower() in ("false", "disabled", "none", "nenhum"):
                apply_this_zoom = False
            else:
                apply_this_zoom = apply_zoom

            if apply_this_zoom:
                anim_id, anim_mat = make_scene_animation(templates, img_dur_us, zoom_threshold_us, forced_anim)
                material_animations.append(anim_mat)
                img_refs.append(anim_id)

            # Transição na imagem para a próxima cena
            c_trans = clip.get("transition")
            c_trans_dur = clip.get("transitionDuration")
            c_trans_str = str(c_trans).strip().lower() if c_trans is not None else ""
            if c_trans_str in ("false", "disabled", "sem_transicao"):
                has_trans = False
                trans_dur = transition_dur_us
            elif c_trans_str and c_trans_str not in ("none", "nenhum", "null", "undefined"):
                has_trans = not is_last_scene
                trans_dur = sec_to_us(float(c_trans_dur)) if c_trans_dur and float(c_trans_dur) > 0 else transition_dur_us
            else:
                has_trans = apply_transition and not is_last_scene
                trans_dur = sec_to_us(float(c_trans_dur)) if c_trans_dur and float(c_trans_dur) > 0 else transition_dur_us

            if has_trans:
                trans_id, trans_mat = make_scene_transition(templates, trans_dur)
                transitions_list.append(trans_mat)
                img_refs.append(trans_id)

            c_fit = clip.get("fit", companion_m.get("fit", "contain"))
            c_sx, c_sy, c_tx, c_ty = compute_fit_scale(c_fit, c_mat["width"], c_mat["height"], width, height)

            video_segments.append(create_segment_dict(
                seg_id=new_uuid(),
                material_id=c_mid,
                start_us=img_start_us,
                target_dur_us=img_dur_us,
                source_start_us=0,
                source_dur_us=img_dur_us,
                speed_val=1.0,
                scale_x=c_sx, scale_y=c_sy,
                trans_x=c_tx, trans_y=c_ty,
                extra_refs=img_refs,
                volume=0.0
            ))

        else:
            # ── Segmento Único Normal (Foto, Vídeo cortado, ou Vídeo desacelerado em até 40%) ──
            mat_dur_us = sec_to_us(orig_dur_s) if is_video and orig_dur_s > 0 else dur_us
            mat_id, mat_dict = register_material(m, is_video, file_path, mat_dur_us)

            extra_refs = make_speed_and_audio(speed_val)

            # Zoom / Quicar
            c_anim = clip.get("animation", clip.get("zoom"))
            if c_anim is not None and str(c_anim).lower() in ("quicar", "bounce"):
                apply_this_zoom = not is_video
                forced_anim = "quicar_animation"
            elif c_anim is not None and str(c_anim).lower() in ("zoom", "zoom1"):
                apply_this_zoom = not is_video
                forced_anim = "zoom_animation"
            elif c_anim is not None and str(c_anim).lower() in ("false", "disabled", "none", "nenhum"):
                apply_this_zoom = False
                forced_anim = None
            else:
                apply_this_zoom = apply_zoom and not is_video
                forced_anim = None

            if apply_this_zoom:
                anim_id, anim_mat = make_scene_animation(templates, dur_us, zoom_threshold_us, forced_anim)
                material_animations.append(anim_mat)
                extra_refs.append(anim_id)

            # Transição
            c_trans = clip.get("transition")
            c_trans_dur = clip.get("transitionDuration")
            c_trans_str = str(c_trans).strip().lower() if c_trans is not None else ""
            if c_trans_str in ("false", "disabled", "sem_transicao"):
                has_trans = False
                trans_dur = transition_dur_us
            elif c_trans_str and c_trans_str not in ("none", "nenhum", "null", "undefined"):
                has_trans = not is_last_scene
                trans_dur = sec_to_us(float(c_trans_dur)) if c_trans_dur and float(c_trans_dur) > 0 else transition_dur_us
            else:
                has_trans = apply_transition and not is_last_scene
                trans_dur = sec_to_us(float(c_trans_dur)) if c_trans_dur and float(c_trans_dur) > 0 else transition_dur_us

            if has_trans:
                trans_id, trans_mat = make_scene_transition(templates, trans_dur)
                transitions_list.append(trans_mat)
                extra_refs.append(trans_id)

            # Fades
            fin_s = float(clip.get("fadeIn") or 0.0)
            fout_s = float(clip.get("fadeOut") or 0.0)
            if fin_s > 0 or fout_s > 0:
                fade_id = new_uuid()
                audio_fades_list.append({
                    "fade_in_duration": sec_to_us(fin_s),
                    "fade_out_duration": sec_to_us(fout_s),
                    "id": fade_id,
                    "type": "audio_fade"
                })
                extra_refs.append(fade_id)

            fit = clip.get("fit", m.get("fit", "contain"))
            sx, sy, tx, ty = compute_fit_scale(fit, mat_dict["width"], mat_dict["height"], width, height)

            video_segments.append(create_segment_dict(
                seg_id=new_uuid(),
                material_id=mat_id,
                start_us=start_us,
                target_dur_us=dur_us,
                source_start_us=sec_to_us(source_start_s),
                source_dur_us=sec_to_us(source_dur_s),
                speed_val=speed_val,
                scale_x=sx, scale_y=sy,
                trans_x=tx, trans_y=ty,
                extra_refs=extra_refs,
                volume=vol
            ))

    # ── #3 Múltiplos áudios ───────────────────────────────────────────────────
    audio_materials  = []
    audio_tracks     = []

    for aidx, ainfo in enumerate(combined_audio_files):
        if progress_cb:
            progress_cb(len(resolved_clips) + aidx, total_steps)

        afile  = ainfo["name"]
        avol   = ainfo.get("volume", 1.0)
        apath  = fix_path(media_folder, afile)

        audio_dur_us = get_audio_duration_us(apath) or total_us
        custom_dur_s = ainfo.get("duration", 0.0)
        custom_start_s = ainfo.get("start_time", 0.0)

        audio_seg_dur_us = sec_to_us(custom_dur_s) if custom_dur_s > 0 else min(audio_dur_us, total_us)
        audio_seg_start_us = sec_to_us(custom_start_s)

        audio_mat_id   = new_uuid()
        audio_seg_id   = new_uuid()
        audio_speed_id = new_uuid()
        audio_sc_id    = new_uuid()
        audio_vs_id    = new_uuid()

        audio_local_id = str(uuid.uuid4()).lower()
        audio_materials.append({
            "app_id": 0, "category_id": "", "category_name": "",
            "check_flag": 1, "duration": audio_dur_us,
            "effect_id": "", "formula_id": "", "id": audio_mat_id,
            "intensifies_path": "", "local_material_id": audio_local_id,
            "music_id": "", "name": afile,
            "path": apath,
            "query": "", "request_id": "", "resource_id": "",
            "search_id": "", "source_platform": 0,
            "team_id": "", "text": "", "tone_folder_path": "",
            "tts_auto_update": False, "type": "music",
            "video_id": "", "wave_points": [],
        })

        audio_segment = {
            "caption_info": None, "cartoon": False,
            "clip": {"alpha": 1.0,
                     "flip": {"horizontal": False, "vertical": False},
                     "rotation": 0.0,
                     "scale": {"x": 1.0, "y": 1.0},
                     "translation": {"x": 0.0, "y": 0.0}},
            "common_keyframes": [],
            "desc": "",
            "enable_adjust": True, "enable_color_curves": True,
            "enable_color_wheels": True, "enable_lut": True,
            "extra_material_refs": [audio_speed_id, audio_sc_id, audio_vs_id],
            "group_id": "",
            "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000},
            "id": audio_seg_id,
            "intensifies_audio": False,
            "is_loop": False, "is_placeholder": False,
            "keyframe_refs": [],
            "last_nonzero_volume": avol,
            "lyric_keyframes": None,
            "material_id": audio_mat_id,
            "raw_segment_id": "",
            "render_index": 0,
            "render_timerange": {"duration": 0, "start": 0},
            "responsive_layout": {"enable": False, "horizontal_pos_layout": 0,
                                  "size_layout": 0, "target_follow": "",
                                  "vertical_pos_layout": 0},
            "reverse": False,
            "source": "segmentsourcenormal",
            "source_timerange": {"duration": audio_seg_dur_us, "start": 0},
            "speed": 1.0, "state": 0,
            "target_timerange": {"start": audio_seg_start_us, "duration": audio_seg_dur_us},
            "template_id": "", "template_scene": "default",
            "track_attribute": 0, "track_render_index": 0,
            "uniform_scale": {"on": True, "value": 1.0},
            "visible": True, "volume": avol,
        }

        speeds.append({"curve_speed": None, "id": audio_speed_id, "mode": 0,
                        "speed": 1.0, "type": "speed"})
        sound_channels.append({"audio_channel_mapping": 0, "id": audio_sc_id,
                                "is_config_open": False, "type": "none"})
        vocal_seps.append({"choice": 0, "id": audio_vs_id, "production_path": "",
                           "removed_sounds": [], "time_range": None,
                           "type": "vocal_separation"})

        track_id = new_uuid()
        audio_tracks.append({
            "attribute": 0, "flag": 0, "id": track_id,
            "is_default_name": True, "name": "",
            "type": "audio",
            "segments": [audio_segment],
        })
        total_us = max(total_us, audio_seg_start_us + audio_seg_dur_us)

    # ── Canvas / Placeholder ──────────────────────────────────────────────────
    canvas_id = new_uuid()
    canvas = {"album_image": "", "blur": 0.0, "color": "", "id": canvas_id,
              "image": "", "image_id": "", "image_name": "",
              "source_platform": 0, "team_id": "", "type": "canvas_color"}

    ph_id = new_uuid()
    placeholder = {"error_path": "", "error_text": "", "id": ph_id,
                   "meta_type": "none", "res_path": "", "res_text": "",
                   "type": "placeholder_info"}

    # ── Filtro VHS e marca do canal (opcionais, cobrindo o vídeo inteiro) ─────
    extra_tracks = []
    video_effects_list = []
    text_effects_list = []
    texts_list = []
    text_templates_list = []

    if apply_vhs:
        vhs_track, vhs_mat = build_vhs_layer(templates, total_us)
        if vhs_track:
            extra_tracks.append(vhs_track)
            video_effects_list.append(vhs_mat)

    if apply_watermark:
        (wm_track, wm_text_mat, wm_template_mat,
         wm_text_effect_mat, wm_decorative_anims) = build_watermark_layer(
            templates, total_us, watermark_text)
        if wm_track:
            extra_tracks.append(wm_track)
            texts_list.append(wm_text_mat)
            text_templates_list.append(wm_template_mat)
            text_effects_list.append(wm_text_effect_mat)
            material_animations.extend(wm_decorative_anims)

    # ── Tracks ────────────────────────────────────────────────────────────────
    video_track_id = new_uuid()
    tracks = [
        {"attribute": 0, "flag": 0, "id": video_track_id,
         "is_default_name": True, "name": "", "type": "video",
         "segments": video_segments},
        *audio_tracks,
        *extra_tracks,
    ]

    # ── Draft final ───────────────────────────────────────────────────────────
    draft = {
        "canvas_config": {"height": height, "ratio": "original", "width": width},
        "color_space": 0,
        "config": {
            "adjust_max_index": 1, "attachment_info": [],
            "combination_max_index": 1, "export_range": None,
            "extract_audio_last_index": 1,
            "lyrics_recognition_id": "", "lyrics_sync": True, "lyrics_taskinfo": [],
            "maintrack_adsorb": True, "material_save_mode": 0,
            "multi_language_current": "none", "multi_language_list": [],
            "multi_language_main": "none", "multi_language_mode": "none",
            "original_sound_last_index": 1, "record_audio_last_index": 1,
            "sticker_max_index": 1, "subtitle_keywords_config": None,
            "subtitle_recognition_id": "", "subtitle_sync": True,
            "subtitle_taskinfo": [], "system_font_list": [],
            "use_float_render": False, "video_mute": False, "zoom_info_params": None,
        },
        "cover": None,
        "create_time": now,
        "duration": total_us,
        "extra_info": None,
        "fps": float(fps),
        "free_render_index_mode_on": False,
        "group_container": None,
        "id": new_uuid(),
        "is_drop_frame_timecode": False,
        "keyframe_graph_list": [],
        "keyframes": {"adjusts": [], "audios": [], "effects": [], "filters": [],
                      "handwrites": [], "stickers": [], "texts": [], "videos": []},
        "last_modified_platform": {
            "app_id": 359289, "app_source": "cc", "app_version": "6.4.0",
            "device_id": "", "hard_disk_id": "", "mac_address": "",
            "os": "windows", "os_version": "",
        },
        "lyrics_effects": [],
        "materials": {
            "ai_translates": [], "audio_balances": [], "audio_effects": [],
            "audio_fades": audio_fades_list, "audio_track_indexes": [],
            "audios": audio_materials,
            "beats": [], "canvases": [canvas], "chromas": [],
            "color_curves": [], "common_mask": [],
            "digital_human_model_dressing": [], "digital_humans": [],
            "drafts": [], "effects": text_effects_list, "flowers": [], "green_screens": [],
            "handwrites": [], "hsl": [], "images": [], "log_color_wheels": [],
            "loudnesses": [], "manual_beautys": [], "manual_deformations": [],
            "material_animations": material_animations, "material_colors": [],
            "multi_language_refs": [],
            "placeholder_infos": [placeholder],
            "placeholders": [], "plugin_effects": [],
            "primary_color_wheels": [], "realtime_denoises": [],
            "shapes": [], "smart_crops": [], "smart_relights": [],
            "sound_channel_mappings": sound_channels,
            "speeds": speeds,
            "stickers": [], "tail_leaders": [], "text_templates": text_templates_list,
            "texts": texts_list, "time_marks": [], "transitions": transitions_list,
            "video_effects": video_effects_list, "video_trackings": [],
            "videos": video_materials,
            "vocal_beautifys": [],
            "vocal_separations": vocal_seps,
        },
        "mutable_config": None,
        "name": project_name,
        "new_version": "110.0.0",
        "path": "",
        "platform": {
            "app_id": 359289, "app_source": "cc", "app_version": "6.4.0",
            "device_id": "", "hard_disk_id": "", "mac_address": "",
            "os": "windows", "os_version": "",
        },
        "relationships": [],
        "render_index_track_mode_on": False,
        "retouch_cover": None,
        "source": "default",
        "static_cover_image_path": "",
        "time_marks": None,
        "tracks": tracks,
        "uneven_animation_template_info": {
            "action_infos": [], "anim_id": "", "panel_infos": [],
        },
        "update_time": now,
        "version": 360000,
    }

    if progress_cb:
        progress_cb(total_steps, total_steps)

    return draft


def build_meta_info(project_name, total_us, draft_folder="", drafts_root="", draft_content=None):
    now = int(datetime.now().timestamp())
    now_us = int(datetime.now().timestamp() * 1_000_000)

    draft_materials = [
        {"type": 0, "value": []},
        {"type": 1, "value": []},
        {"type": 2, "value": []},
        {"type": 3, "value": []},
        {"type": 6, "value": []},
        {"type": 7, "value": []},
        {"type": 8, "value": []},
    ]

    if draft_content and "materials" in draft_content:
        for v in draft_content["materials"].get("videos", []):
            mat_type = v.get("type", "photo")
            v_path = v.get("path", "").replace("\\", "/")
            v_name = v.get("material_name", "")
            local_id = v.get("local_material_id") or v.get("id") or str(uuid.uuid4()).lower()
            dur = v.get("duration", 0)
            draft_materials[0]["value"].append({
                "ai_group_type": "",
                "create_time": now,
                "duration": dur,
                "extra_info": v_name,
                "file_Path": v_path,
                "height": v.get("height", 1080),
                "id": local_id,
                "import_time": now,
                "import_time_ms": -1,
                "item_source": 1,
                "md5": "",
                "metetype": "video" if mat_type == "video" else "photo",
                "roughcut_time_range": {"duration": dur, "start": 0},
                "sub_time_range": {"duration": -1, "start": -1},
                "type": 0,
                "width": v.get("width", 1920),
            })

        for a in draft_content["materials"].get("audios", []):
            a_path = a.get("path", "").replace("\\", "/")
            a_name = a.get("name", "")
            local_id = a.get("local_material_id") or a.get("id") or str(uuid.uuid4()).lower()
            dur = a.get("duration", 0)
            draft_materials[1]["value"].append({
                "ai_group_type": "",
                "create_time": now,
                "duration": dur,
                "extra_info": a_name,
                "file_Path": a_path,
                "height": 0,
                "id": local_id,
                "import_time": now,
                "import_time_ms": -1,
                "item_source": 1,
                "md5": "",
                "metetype": "music",
                "roughcut_time_range": {"duration": -1, "start": -1},
                "sub_time_range": {"duration": -1, "start": -1},
                "type": 1,
                "width": 0,
            })

    fold_path = str(draft_folder).replace("\\", "/") if draft_folder else ""
    root_path = str(drafts_root).replace("/", "\\") if drafts_root else ""

    return {
        "cloud_package_completed_time": "",
        "draft_cloud_capcut_purchase_info": "",
        "draft_cloud_last_action_download": False,
        "draft_cloud_package_type": "",
        "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "draft_cover.jpg",
        "draft_deeplink_url": "",
        "draft_enterprise_info": {
            "draft_enterprise_extra": "",
            "draft_enterprise_id": "",
            "draft_enterprise_name": "",
            "enterprise_material": [],
        },
        "draft_fold_path": fold_path,
        "draft_id": new_uuid(),
        "draft_is_ae_produce": False,
        "draft_is_ai_packaging_used": False,
        "draft_is_ai_shorts": False,
        "draft_is_ai_translate": False,
        "draft_is_article_video_draft": False,
        "draft_is_from_deeplink": "false",
        "draft_is_invisible": False,
        "draft_materials": draft_materials,
        "draft_materials_copied_info": [],
        "draft_name": project_name,
        "draft_need_rename_folder": False,
        "draft_new_version": "",
        "draft_removable_storage_device": "",
        "draft_root_path": root_path,
        "draft_segment_extra_info": [],
        "draft_timeline_materials_size_": 0,
        "draft_type": "",
        "tm_draft_cloud_completed": "",
        "tm_draft_cloud_modified": 0,
        "tm_draft_cloud_space_id": -1,
        "tm_draft_create": now_us,
        "tm_draft_modified": now_us,
        "tm_draft_removed": 0,
        "tm_duration": total_us,
    }


# ─── Compactação de Áudios de TTS ──────────────────────────────────────────

def list_capcut_drafts(drafts_dir: str = "") -> list:
    """Retorna lista de drafts encontrados ordenados por data decrescente."""
    if not drafts_dir:
        drafts_dir = find_capcut_drafts()
    p_dir = Path(drafts_dir)
    if not p_dir.is_dir():
        return []

    results = []
    for d in p_dir.iterdir():
        if d.is_dir() and (d / "draft_content.json").is_file():
            try:
                mtime = d.stat().st_mtime
                dt_str = datetime.fromtimestamp(mtime).strftime("%d/%m/%Y %H:%M")
                name = d.name
                meta_p = d / "draft_meta_info.json"
                if meta_p.is_file():
                    try:
                        with open(meta_p, "r", encoding="utf-8") as f:
                            m_data = json.load(f)
                            name = m_data.get("draft_name") or d.name
                    except Exception:
                        pass
                results.append({
                    "name": name,
                    "folder_name": d.name,
                    "path": str(d).replace("\\", "/"),
                    "mtime": mtime,
                    "date": dt_str
                })
            except Exception:
                pass

    results.sort(key=lambda x: x["mtime"], reverse=True)
    return results


def inspect_draft_audios(draft_folder: str) -> dict:
    """Inspeciona as trilhas de áudio e legendas de um draft."""
    p = Path(draft_folder)
    content_p = p / "draft_content.json"
    if not content_p.is_file():
        return {"ok": False, "error": "draft_content.json não encontrado."}

    try:
        with open(content_p, "r", encoding="utf-8") as f:
            draft = json.load(f)
    except Exception as e:
        return {"ok": False, "error": f"Erro ao ler JSON: {e}"}

    tracks = draft.get("tracks", [])
    audio_info = []
    text_count = 0
    for i, t in enumerate(tracks):
        ttype = t.get("type")
        segs = t.get("segments", [])
        if ttype == "audio":
            audio_info.append({
                "index": i,
                "segments": len(segs),
                "is_tts_candidate": len(segs) > 1
            })
        elif ttype == "text":
            text_count += len(segs)

    dur_s = draft.get("duration", 0) / 1_000_000
    has_backup = (p / "draft_content.json.bak").is_file() or (p / "draft_content.json.original").is_file()

    return {
        "ok": True,
        "name": draft.get("name", p.name),
        "duration_s": round(dur_s, 2),
        "audio_tracks": audio_info,
        "subtitles_count": text_count,
        "has_backup": has_backup,
    }


def compact_draft_audios(draft_folder: str, gap_seconds: float = 0.0,
                         start_offset: float = 0.0, sync_subtitles: bool = True,
                         track_index: int = -1,
                         trim_silence: bool = False,
                         trim_start_s: float = 0.10,
                         trim_end_s: float = 0.15,
                         micro_fade: bool = False,
                         micro_fade_ms: float = 15.0) -> dict:
    """
    Compacta os segmentos de áudio (geralmente TTS) de um projeto do CapCut,
    eliminando os gaps de silêncio entre as falas.
    Opcionalmente aplica Trim nas pontas de cada fala e micro-fade anti-estalo.
    """
    p_folder = Path(draft_folder)
    content_file = p_folder / "draft_content.json"
    meta_file = p_folder / "draft_meta_info.json"

    if not content_file.is_file():
        return {"ok": False, "error": f"draft_content.json não encontrado em: {draft_folder}"}

    # Backup de segurança: o backup original (.bak / .original) só é gravado na PRIMEIRA compactação
    bak_orig = p_folder / "draft_content.json.original"
    bak_file = p_folder / "draft_content.json.bak"
    bak_meta_orig = p_folder / "draft_meta_info.json.original"
    bak_meta = p_folder / "draft_meta_info.json.bak"

    is_first_backup = not (bak_orig.is_file() or bak_file.is_file())
    backup_path = str(bak_file)

    if is_first_backup:
        try:
            shutil.copy2(content_file, bak_file)
            shutil.copy2(content_file, bak_orig)
            if meta_file.is_file():
                shutil.copy2(meta_file, bak_meta)
                shutil.copy2(meta_file, bak_meta_orig)
        except Exception as e:
            print(f"[Backup warning] {e}")
    else:
        # Nas compactações seguintes, mantém histórico com data e hora
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        hist_content = p_folder / f"draft_content_backup_{timestamp}.json"
        backup_path = str(hist_content)
        try:
            shutil.copy2(content_file, hist_content)
            if meta_file.is_file():
                hist_meta = p_folder / f"draft_meta_info_backup_{timestamp}.json"
                shutil.copy2(meta_file, hist_meta)
        except Exception as e:
            print(f"[History backup warning] {e}")

    with open(content_file, "r", encoding="utf-8") as f:
        draft = json.load(f)

    tracks = draft.get("tracks", [])
    audio_tracks = [(i, t) for i, t in enumerate(tracks) if t.get("type") == "audio" and t.get("segments")]

    if not audio_tracks:
        return {"ok": False, "error": "Nenhuma trilha de áudio com segmentos foi encontrada neste projeto."}

    # Seleciona a trilha
    if track_index >= 0 and track_index < len(tracks) and tracks[track_index].get("type") == "audio":
        selected_idx = track_index
        target_track = tracks[track_index]
    else:
        best_idx = audio_tracks[0][0]
        max_segs = 0
        mat_path_map = {m.get("id"): str(m.get("path", "")).lower() for m in draft.get("materials", {}).get("audios", [])}
        for i, t in audio_tracks:
            segs = t.get("segments", [])
            score = len(segs)
            if any("textreading" in mat_path_map.get(s.get("material_id", ""), "") for s in segs):
                score += 1000
            if score > max_segs:
                max_segs = score
                best_idx = i
        selected_idx = best_idx
        target_track = tracks[selected_idx]

    segments = target_track.get("segments", [])
    if len(segments) <= 1:
        return {"ok": False, "error": f"A trilha de áudio selecionada contém apenas {len(segments)} clipe. Não há gaps para compactar."}

    # Ordena os segmentos cronologicamente pelo start original
    segments.sort(key=lambda s: s.get("target_timerange", {}).get("start", 0))

    gap_us = sec_to_us(gap_seconds)
    orig_first_start = segments[0]["target_timerange"]["start"]
    cur_start = orig_first_start if start_offset is None else sec_to_us(start_offset)

    orig_last_end = max(s["target_timerange"]["start"] + s["target_timerange"]["duration"] for s in segments)
    orig_total_span_us = orig_last_end - orig_first_start

    trim_s_us = sec_to_us(trim_start_s) if trim_silence else 0
    trim_e_us = sec_to_us(trim_end_s) if trim_silence else 0
    fade_dur_us = sec_to_us(micro_fade_ms / 1000.0) if micro_fade else 0

    fade_materials = draft.setdefault("materials", {}).setdefault("audio_fades", [])

    shift_map = []  # [(old_s, old_e, new_s, new_e)]

    for seg in segments:
        tr = seg.setdefault("target_timerange", {})
        sr = seg.setdefault("source_timerange", {})
        old_s = tr.get("start", 0)
        dur = tr.get("duration", 0)
        old_e = old_s + dur
        sr_start = sr.get("start", 0)
        sr_dur = sr.get("duration", dur)

        # 1. Trim inteligente de silêncio nas pontas
        if trim_silence and (trim_s_us > 0 or trim_e_us > 0):
            # Preserva pelo menos 200ms de clipe
            if dur > (trim_s_us + trim_e_us + 200_000):
                sr["start"] = sr_start + trim_s_us
                sr["duration"] = sr_dur - (trim_s_us + trim_e_us)
                dur = sr["duration"]
                tr["duration"] = dur

        # 2. Micro fade anti-estalo nas junções
        if micro_fade and fade_dur_us > 0:
            fade_id = new_uuid()
            fade_materials.append({
                "fade_in_duration": fade_dur_us,
                "fade_out_duration": fade_dur_us,
                "id": fade_id,
                "type": "audio_fade"
            })
            extra_refs = seg.setdefault("extra_material_refs", [])
            seg["extra_material_refs"] = list(extra_refs) + [fade_id]

        tr["start"] = cur_start
        new_e = cur_start + dur
        shift_map.append((old_s, old_e, cur_start, new_e))

        cur_start += dur + gap_us

    compact_last_end = cur_start - gap_us
    saved_us = orig_last_end - compact_last_end

    # Sincronização de Legendas (trilhas de texto)
    synced_texts = 0
    if sync_subtitles:
        text_tracks = [t for t in tracks if t.get("type") == "text" and t.get("segments")]
        for tt in text_tracks:
            text_segs = tt.get("segments", [])
            if len(text_segs) == len(segments):
                text_segs.sort(key=lambda s: s.get("target_timerange", {}).get("start", 0))
                for s_txt, (old_s, old_e, new_s, new_e) in zip(text_segs, shift_map):
                    t_tr = s_txt.setdefault("target_timerange", {})
                    t_tr["start"] = new_s
                    t_tr["duration"] = (new_e - new_s)
                    synced_texts += 1
            else:
                for s_txt in text_segs:
                    t_tr = s_txt.setdefault("target_timerange", {})
                    ts = t_tr.get("start", 0)
                    best_match = None
                    min_dist = float("inf")
                    for (old_s, old_e, new_s, new_e) in shift_map:
                        dist = abs(ts - old_s)
                        if dist < min_dist:
                            min_dist = dist
                            best_match = (old_s, old_e, new_s, new_e)
                    if best_match and min_dist <= 3_000_000:
                        old_s, old_e, new_s, new_e = best_match
                        offset_within = ts - old_s
                        t_tr["start"] = max(0, new_s + offset_within)
                        synced_texts += 1

    # Recalcula a duração máxima de todas as tracks do projeto
    max_track_dur = 0
    for t in tracks:
        for s in t.get("segments", []):
            st = s.get("target_timerange", {}).get("start", 0)
            d = s.get("target_timerange", {}).get("duration", 0)
            if st + d > max_track_dur:
                max_track_dur = st + d

    draft["duration"] = max_track_dur

    with open(content_file, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)

    if meta_file.is_file():
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
            meta["tm_duration"] = max_track_dur
            meta["tm_draft_modified"] = int(datetime.now().timestamp() * 1_000_000)
            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Meta update warning] {e}")

    orig_s = orig_total_span_us / 1_000_000
    comp_s = (compact_last_end - (orig_first_start if start_offset is None else sec_to_us(start_offset))) / 1_000_000
    saved_s = max(0.0, saved_us / 1_000_000)

    return {
        "ok": True,
        "segments_count": len(segments),
        "synced_texts": synced_texts,
        "original_duration_s": round(orig_s, 2),
        "compact_duration_s": round(comp_s, 2),
        "saved_seconds": round(saved_s, 2),
        "backup_path": backup_path,
        "is_first_backup": is_first_backup,
        "new_project_duration_s": round(max_track_dur / 1_000_000, 2),
        "trim_applied": trim_silence,
        "fade_applied": micro_fade,
    }


def restore_draft_backup(draft_folder: str) -> dict:
    """Restaura o backup original do draft_content.json e draft_meta_info.json."""
    p_folder = Path(draft_folder)
    bak_content = p_folder / "draft_content.json.original"
    if not bak_content.is_file():
        bak_content = p_folder / "draft_content.json.bak"
    if not bak_content.is_file():
        return {"ok": False, "error": "Nenhum arquivo de backup original encontrado neste projeto."}

    target_content = p_folder / "draft_content.json"
    shutil.copy2(bak_content, target_content)

    bak_meta = p_folder / "draft_meta_info.json.original"
    if not bak_meta.is_file():
        bak_meta = p_folder / "draft_meta_info.json.bak"
    target_meta = p_folder / "draft_meta_info.json"
    if bak_meta.is_file():
        try:
            shutil.copy2(bak_meta, target_meta)
        except Exception:
            pass

    return {"ok": True, "message": "Backup original restaurado com sucesso!"}


def set_capcut_path(path: str) -> dict:
    """Valida que o arquivo existe, grava 'capcut_path' no config e retorna status."""
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": f"Arquivo executável do CapCut não encontrado ou inválido: {path}"}
    try:
        cfg = load_config()
        cfg["capcut_path"] = str(Path(path).resolve())
        save_config(cfg)
        return {"ok": True, "capcut_path": cfg["capcut_path"]}
    except Exception as e:
        return {"ok": False, "error": f"Erro ao salvar configuração: {e}"}


def launch_capcut() -> dict:
    """
    Localiza e abre o CapCut Desktop nos locais padrão de instalação:
      a) %LOCALAPPDATA%\\CapCut\\Apps\\*\\CapCut.exe
      b) atalhos '*CapCut*.lnk' na área de trabalho do usuário atual (Path.home() / 'Desktop')
      c) %LOCALAPPDATA%\\CapCut\\CapCut.exe
    Se não encontrar, verifica 'capcut_path' em converter_config.json.
    Se ainda não houver, retorna {"ok": False, "need_path": True, "error": "..."}.
    """
    target = None

    # a) %LOCALAPPDATA%\CapCut\Apps\*\CapCut.exe (usar os.path.expandvars)
    local_app_data = os.path.expandvars(r"%LOCALAPPDATA%")
    if local_app_data and local_app_data != "%LOCALAPPDATA%":
        try:
            apps_dir = Path(local_app_data) / "CapCut" / "Apps"
            if apps_dir.is_dir():
                for ae in sorted(apps_dir.glob("*/CapCut.exe"), reverse=True):
                    if ae.is_file():
                        target = str(ae)
                        break
        except Exception:
            pass

    # b) atalhos "*CapCut*.lnk" na área de trabalho do usuário atual (Path.home() / "Desktop")
    if not target:
        try:
            desktop = Path.home() / "Desktop"
            if desktop.is_dir():
                for lnk in desktop.glob("*CapCut*.lnk"):
                    if lnk.is_file():
                        target = str(lnk)
                        break
        except Exception:
            pass

    # c) %LOCALAPPDATA%\CapCut\CapCut.exe
    if not target and local_app_data and local_app_data != "%LOCALAPPDATA%":
        try:
            c_exe = Path(local_app_data) / "CapCut" / "CapCut.exe"
            if c_exe.is_file():
                target = str(c_exe)
        except Exception:
            pass

    # Se não encontrar nos locais padrão, verificar a chave "capcut_path" em converter_config.json
    if not target:
        cfg = load_config()
        saved = (cfg.get("capcut_path") or "").strip()
        if saved and os.path.exists(saved):
            target = saved

    if target:
        try:
            if hasattr(os, "startfile"):
                os.startfile(target)
            else:
                subprocess.Popen([target])
            return {"ok": True, "path": target}
        except Exception as e:
            return {"ok": False, "error": f"Erro ao abrir CapCut ({target}): {e}"}

    return {
        "ok": False,
        "need_path": True,
        "error": "Não foi possível localizar o CapCut nos locais padrão. Por favor, aponte o arquivo CapCut.exe manualmente."
    }


# ─── Extração e Concatenação de Áudio (MP3 / WAV via FFmpeg) ─────────────────

def get_ffmpeg_bin() -> str:
    """
    Retorna o caminho para o executável do ffmpeg somente na pasta do próprio app.
    Se não existir, retorna string vazia e registra um aviso claro no log.
    """
    app_ffmpeg = Path(__file__).parent / "ffmpeg.exe"
    if app_ffmpeg.is_file():
        return str(app_ffmpeg)

    try:
        print("[AVISO] ffmpeg.exe não foi encontrado na pasta do aplicativo.")
    except Exception:
        pass
    return ""


def extract_audio_from_video(video_path: str, output_path: str, audio_format: str = "mp3", bitrate: str = "320k") -> dict:
    """Extrai a trilha sonora de um arquivo de vídeo (.mp4, .mov, etc) para .mp3 ou .wav."""
    if not video_path or not os.path.isfile(video_path):
        return {"ok": False, "error": f"Arquivo de vídeo não encontrado: {video_path}"}

    ffmpeg = get_ffmpeg_bin()
    if not ffmpeg:
        return {"ok": False, "error": "Executável FFmpeg não encontrado."}

    p_out = Path(output_path)
    p_out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [ffmpeg, "-y", "-i", video_path, "-vn"]
    fmt = audio_format.lower().strip()
    if fmt == "wav":
        cmd.extend(["-c:a", "pcm_s16le"])
    else:
        br = bitrate if bitrate else "320k"
        cmd.extend(["-c:a", "libmp3lame", "-b:a", br])
    cmd.append(str(p_out))

    creationflags = 0x08000000 if platform.system() == "Windows" else 0  # CREATE_NO_WINDOW
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=creationflags)
        if res.returncode != 0:
            return {"ok": False, "error": f"Erro no FFmpeg: {res.stderr[-400:]}"}
        if not p_out.is_file() or p_out.stat().st_size == 0:
            return {"ok": False, "error": "Arquivo de áudio não foi gerado."}

        size_mb = round(p_out.stat().st_size / (1024 * 1024), 2)
        return {
            "ok": True,
            "output_path": str(p_out).replace("\\", "/"),
            "size_mb": size_mb,
            "format": fmt
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def resolve_draft_material_path(raw_p: str, p_folder: Path) -> str:
    """Resolve caminhos de materiais do CapCut que podem conter placeholders ou caminhos relativos."""
    if not raw_p:
        return ""
    folder_str = str(p_folder).replace("\\", "/")
    # Substitui placeholder de draft (ex: ##_draftpath_placeholder_UUID_##)
    p_str = re.sub(r'##_draftpath_placeholder_[^#]+_##', lambda _: folder_str, raw_p)
    if Path(p_str).exists():
        return str(Path(p_str)).replace("\\", "/")

    fname = Path(raw_p).name
    # Fallback 1: dentro de textReading
    cand1 = p_folder / "textReading" / fname
    if cand1.exists():
        return str(cand1).replace("\\", "/")
    # Fallback 2: relativo à pasta do projeto
    cand2 = p_folder / raw_p.lstrip("/\\")
    if cand2.exists():
        return str(cand2).replace("\\", "/")
    # Fallback 3: dentro de Resources
    cand3 = p_folder / "Resources" / fname
    if cand3.exists():
        return str(cand3).replace("\\", "/")
    return p_str


def get_tts_segments_from_draft(draft_folder: str) -> list:
    """Extrai todos os segmentos de áudio de narração TTS de um projeto do CapCut em ordem cronológica."""
    p_folder = Path(draft_folder)
    content_file = p_folder / "draft_content.json"
    if not content_file.is_file():
        return []

    files_to_check = [content_file] + list((p_folder / "subdraft").glob("*/draft_content.json"))
    best_segments = None
    best_mat_map = {}
    best_score = -1

    for cf in files_to_check:
        try:
            with open(cf, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue

        mat_map = {}
        for m in d.get("materials", {}).get("audios", []):
            mat_map[m.get("id")] = resolve_draft_material_path(m.get("path", ""), p_folder)

        for comb in d.get("materials", {}).get("drafts", []):
            inner = comb.get("draft", {})
            for m in inner.get("materials", {}).get("audios", []):
                mat_map[m.get("id")] = resolve_draft_material_path(m.get("path", ""), p_folder)
            for t in inner.get("tracks", []):
                if t.get("type") == "audio":
                    segs = t.get("segments", [])
                    score = len(segs)
                    if any("textreading" in str(mat_map.get(s.get("material_id", ""), "")).lower() for s in segs):
                        score += 1000
                    if score > best_score:
                        best_score = score
                        best_segments = segs
                        best_mat_map = dict(mat_map)

        for t in d.get("tracks", []):
            if t.get("type") == "audio":
                segs = t.get("segments", [])
                score = len(segs)
                if any("textreading" in str(mat_map.get(s.get("material_id", ""), "")).lower() for s in segs):
                    score += 1000
                if score > best_score:
                    best_score = score
                    best_segments = segs
                    best_mat_map = dict(mat_map)

    if not best_segments:
        return []

    best_segments.sort(key=lambda s: s.get("target_timerange", {}).get("start", 0))
    result = []
    for s in best_segments:
        mid = s.get("material_id")
        p = best_mat_map.get(mid, "")
        if p and Path(p).exists():
            tr = s.get("target_timerange", {})
            sr = s.get("source_timerange", {})
            result.append({
                "path": str(Path(p)).replace("\\", "/"),
                "start_us": tr.get("start", 0),
                "dur_us": tr.get("duration", 0),
                "source_start_us": sr.get("start", 0),
                "source_dur_us": sr.get("duration", tr.get("duration", 0))
            })
    return result


def inspect_draft_for_audio_extraction(draft_folder: str) -> dict:
    """Verifica e resume mídias disponíveis para extração em um rascunho do CapCut."""
    if not draft_folder or not os.path.isdir(draft_folder):
        return {"ok": False, "error": "Pasta do projeto não encontrada."}

    p = Path(draft_folder)
    video_files = []
    for ext in ("*.mp4", "*.mov", "*.mkv", "*.webm"):
        video_files.extend([str(f).replace("\\", "/") for f in p.glob(ext)])
        video_files.extend([str(f).replace("\\", "/") for f in (p / "Resources").glob(ext)])

    tts_segs = get_tts_segments_from_draft(draft_folder)
    total_tts_dur_s = sum(s["dur_us"] for s in tts_segs) / 1_000_000

    name = p.name
    meta_f = p / "draft_meta_info.json"
    if meta_f.is_file():
        try:
            with open(meta_f, "r", encoding="utf-8") as f:
                name = json.load(f).get("draft_name", name)
        except Exception:
            pass

    return {
        "ok": True,
        "name": name,
        "video_files": video_files,
        "tts_count": len(tts_segs),
        "tts_duration_s": round(total_tts_dur_s, 2),
        "has_tts": len(tts_segs) > 0,
        "has_video": len(video_files) > 0,
    }


def stitch_tts_from_draft(draft_folder: str, output_path: str, audio_format: str = "mp3",
                          bitrate: str = "320k", gap_seconds: float = 0.0,
                          trim_silence: bool = False, trim_start_s: float = 0.10,
                          trim_end_s: float = 0.15) -> dict:
    """Costura as falas do TTS de um draft em um único arquivo .mp3 ou .wav master."""
    ffmpeg = get_ffmpeg_bin()
    if not ffmpeg:
        return {"ok": False, "error": "Executável FFmpeg não encontrado."}

    segs = get_tts_segments_from_draft(draft_folder)
    if not segs:
        return {"ok": False, "error": "Nenhum clipe de áudio TTS encontrado neste rascunho."}

    p_out = Path(output_path)
    p_out.parent.mkdir(parents=True, exist_ok=True)

    silence_file = None
    creationflags = 0x08000000 if platform.system() == "Windows" else 0

    if gap_seconds > 0.01:
        silence_file = tempfile.NamedTemporaryFile("w", suffix=".wav", delete=False).name
        cmd_silence = [
            ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", str(gap_seconds), silence_file
        ]
        subprocess.run(cmd_silence, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)

    concat_txt = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        trim_s = trim_start_s if trim_silence else 0.0
        trim_e = trim_end_s if trim_silence else 0.0

        for i, s in enumerate(segs):
            file_p = s["path"]
            dur_s = s["dur_us"] / 1_000_000

            concat_txt.write(f"file '{file_p}'\n")
            if trim_silence and (trim_s > 0 or trim_e > 0) and dur_s > (trim_s + trim_e + 0.2):
                concat_txt.write(f"inpoint {trim_s:.3f}\n")
                concat_txt.write(f"outpoint {dur_s - trim_e:.3f}\n")

            if silence_file and i < len(segs) - 1:
                sil_clean = silence_file.replace("\\", "/")
                concat_txt.write(f"file '{sil_clean}'\n")

        concat_txt.close()

        cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", concat_txt.name]
        fmt = audio_format.lower().strip()
        if fmt == "wav":
            cmd.extend(["-c:a", "pcm_s16le"])
        else:
            br = bitrate if bitrate else "320k"
            cmd.extend(["-c:a", "libmp3lame", "-b:a", br])
        cmd.append(str(p_out))

        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=creationflags)
        if res.returncode != 0:
            return {"ok": False, "error": f"Erro ao concatenar no FFmpeg: {res.stderr[-400:]}"}

        if not p_out.is_file() or p_out.stat().st_size == 0:
            return {"ok": False, "error": "O arquivo de áudio final não foi criado."}

        size_mb = round(p_out.stat().st_size / (1024 * 1024), 2)
        return {
            "ok": True,
            "output_path": str(p_out).replace("\\", "/"),
            "size_mb": size_mb,
            "segments_count": len(segs),
            "format": fmt
        }
    finally:
        try:
            if os.path.exists(concat_txt.name):
                os.unlink(concat_txt.name)
            if silence_file and os.path.exists(silence_file):
                os.unlink(silence_file)
        except Exception:
            pass


def natural_sort_key(s):
    """Ordenação alfanumérica natural humana (ex: audio_1, audio_2, ..., audio_10)."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', str(s))]


AUDIO_FOLDER_EXTS = {
    # Áudios
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".wma", ".aiff", ".opus", ".weba",
    # Vídeos (ex: partes de narração baixadas do CapCut Online)
    ".mp4", ".mov", ".avi", ".mkv", ".webm"
}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def get_media_duration_us(path: str):
    """
    Retorna a duração em microssegundos de arquivos de áudio ou vídeo.
    Usa parsing nativo rápido para WAV/MP3, e FFmpeg para MP4/MOV/outros formatos.
    """
    if not path or not os.path.isfile(path):
        return None

    dur = get_audio_duration_us(path)
    if dur:
        return dur

    ffmpeg = get_ffmpeg_bin()
    if ffmpeg:
        try:
            creationflags = 0x08000000 if platform.system() == "Windows" else 0
            res = subprocess.run([ffmpeg, "-i", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=creationflags)
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr)
            if m:
                hours = int(m.group(1))
                mins = int(m.group(2))
                secs = float(m.group(3))
                total_secs = hours * 3600 + mins * 60 + secs
                return int(total_secs * 1_000_000)
        except Exception:
            pass
    return None


def inspect_audio_folder(folder_path: str) -> dict:
    """
    Analisa a pasta em busca de arquivos de áudio e vídeo (ex: MP4 do CapCut Online),
    ordenando-os alfabeticamente de forma natural (1, 2, ..., 10).
    """
    if not folder_path or not os.path.isdir(folder_path):
        return {"ok": False, "error": "Pasta não encontrada."}

    p = Path(folder_path)
    media_files = [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_FOLDER_EXTS]
    media_files.sort(key=lambda x: natural_sort_key(x.name))

    if not media_files:
        return {
            "ok": True,
            "folder": str(p).replace("\\", "/"),
            "files": [],
            "total_count": 0,
            "video_count": 0,
            "audio_count": 0,
            "total_duration_s": 0.0,
            "total_duration_formatted": "0s",
            "total_size_mb": 0.0
        }

    items_data = []
    total_dur_us = 0
    total_bytes = 0
    video_count = 0
    audio_count = 0

    for f in media_files:
        is_vid = f.suffix.lower() in VIDEO_EXTS
        if is_vid:
            video_count += 1
        else:
            audio_count += 1

        dur_us = get_media_duration_us(str(f)) or 0
        sz = f.stat().st_size
        total_dur_us += dur_us
        total_bytes += sz
        dur_s = dur_us / 1_000_000 if dur_us else 0.0
        mins = int(dur_s // 60)
        secs = int(dur_s % 60)
        dur_str = f"{mins}m{secs:02d}s" if mins > 0 else f"{secs}s"
        items_data.append({
            "name": f.name,
            "path": str(f).replace("\\", "/"),
            "is_video": is_vid,
            "size_mb": round(sz / (1024 * 1024), 2),
            "duration_s": round(dur_s, 2),
            "duration_formatted": dur_str
        })

    total_s = total_dur_us / 1_000_000
    t_mins = int(total_s // 60)
    t_secs = int(total_s % 60)
    total_str = f"{t_mins}m{t_secs:02d}s" if t_mins > 0 else f"{t_secs}s"

    return {
        "ok": True,
        "folder": str(p).replace("\\", "/"),
        "files": items_data,
        "total_count": len(items_data),
        "video_count": video_count,
        "audio_count": audio_count,
        "total_duration_s": round(total_s, 2),
        "total_duration_formatted": total_str,
        "total_size_mb": round(total_bytes / (1024 * 1024), 2)
    }


def stitch_audio_folder(folder_path: str = "", file_paths: list = None,
                         output_path: str = "", audio_format: str = "mp3",
                         bitrate: str = "320k", gap_seconds: float = 0.0,
                         trim_silence: bool = False, trim_start_s: float = 0.10,
                         trim_end_s: float = 0.15, reverse_order: bool = False) -> dict:
    """
    Concatena uma lista de arquivos de áudio ou vídeo (.mp4, etc.) de uma pasta
    ou lista de caminhos em ordem alfabética natural. Extrai o áudio e gera um
    arquivo único (.mp3 ou .wav master).
    """
    ffmpeg = get_ffmpeg_bin()
    if not ffmpeg:
        return {"ok": False, "error": "Executável FFmpeg não encontrado."}

    files = []
    if file_paths and isinstance(file_paths, list):
        for fp in file_paths:
            p = Path(fp)
            if p.is_file() and p.suffix.lower() in AUDIO_FOLDER_EXTS:
                files.append(p)
    elif folder_path and os.path.isdir(folder_path):
        p = Path(folder_path)
        files = [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_FOLDER_EXTS]

    if not files:
        return {"ok": False, "error": "Nenhum arquivo de áudio ou vídeo encontrado para juntar."}

    files.sort(key=lambda x: natural_sort_key(x.name), reverse=reverse_order)

    p_out = Path(output_path)
    p_out.parent.mkdir(parents=True, exist_ok=True)

    creationflags = 0x08000000 if platform.system() == "Windows" else 0
    silence_file = None

    if gap_seconds > 0.01:
        silence_file = tempfile.NamedTemporaryFile("w", suffix=".wav", delete=False).name
        cmd_silence = [
            ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", str(gap_seconds), silence_file
        ]
        subprocess.run(cmd_silence, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)

    concat_txt = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        trim_s = trim_start_s if trim_silence else 0.0
        trim_e = trim_end_s if trim_silence else 0.0

        for i, fpath in enumerate(files):
            file_clean = str(fpath).replace("\\", "/")
            dur_us = get_media_duration_us(str(fpath)) or 0
            dur_s = dur_us / 1_000_000 if dur_us else 0.0

            concat_txt.write(f"file '{file_clean}'\n")
            if trim_silence and (trim_s > 0 or trim_e > 0) and dur_s > (trim_s + trim_e + 0.2):
                concat_txt.write(f"inpoint {trim_s:.3f}\n")
                concat_txt.write(f"outpoint {dur_s - trim_e:.3f}\n")

            if silence_file and i < len(files) - 1:
                sil_clean = silence_file.replace("\\", "/")
                concat_txt.write(f"file '{sil_clean}'\n")

        concat_txt.close()

        fmt = audio_format.lower().strip()
        cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", concat_txt.name, "-vn"]
        if fmt == "wav":
            cmd.extend(["-c:a", "pcm_s16le"])
        else:
            br = bitrate if bitrate else "320k"
            cmd.extend(["-c:a", "libmp3lame", "-b:a", br])
        cmd.append(str(p_out))

        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=creationflags)

        # Fallback: Se o concat demuxer falhar (ex: codecs / sample rates divergentes entre arquivos)
        if res.returncode != 0:
            inputs = []
            filter_parts = []
            for idx, fpath in enumerate(files):
                inputs.extend(["-i", str(fpath)])
                dur_us = get_media_duration_us(str(fpath)) or 0
                dur_s = dur_us / 1_000_000 if dur_us else 0.0
                atrim_part = ""
                if trim_silence and (trim_s > 0 or trim_e > 0) and dur_s > (trim_s + trim_e + 0.2):
                    atrim_part = f"atrim=start={trim_s:.3f}:end={(dur_s - trim_e):.3f},asetpts=PTS-STARTPTS,"
                filter_parts.append(f"[{idx}:a]{atrim_part}aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a{idx}];")

            concat_inputs = "".join(f"[a{idx}]" for idx in range(len(files)))
            filter_str = "".join(filter_parts) + f"{concat_inputs}concat=n={len(files)}:v=0:a=1[out]"

            cmd_fallback = [ffmpeg, "-y", *inputs, "-filter_complex", filter_str, "-map", "[out]"]
            if fmt == "wav":
                cmd_fallback.extend(["-c:a", "pcm_s16le"])
            else:
                br = bitrate if bitrate else "320k"
                cmd_fallback.extend(["-c:a", "libmp3lame", "-b:a", br])
            cmd_fallback.append(str(p_out))

            res_fallback = subprocess.run(cmd_fallback, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=creationflags)
            if res_fallback.returncode != 0:
                return {"ok": False, "error": f"Erro no FFmpeg: {res_fallback.stderr[-350:]}"}

        if not p_out.is_file() or p_out.stat().st_size == 0:
            return {"ok": False, "error": "O arquivo de áudio unificado não foi gerado."}

        size_mb = round(p_out.stat().st_size / (1024 * 1024), 2)
        total_out_dur_us = get_media_duration_us(str(p_out)) or 0
        total_out_s = round(total_out_dur_us / 1_000_000, 1)

        return {
            "ok": True,
            "output_path": str(p_out).replace("\\", "/"),
            "size_mb": size_mb,
            "duration_s": total_out_s,
            "files_count": len(files),
            "format": fmt
        }
    finally:
        try:
            if os.path.exists(concat_txt.name):
                os.unlink(concat_txt.name)
            if silence_file and os.path.exists(silence_file):
                os.unlink(silence_file)
        except Exception:
            pass


# ─── Interface ───────────────────────────────────────────────────────────────

AUDIO_EXTS = {".mp3", ".MP3", ".wav", ".WAV"}
IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
IMAGE_EXTS  = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


if __name__ == "__main__":
    try:
        import converter_gui
        converter_gui.main()
    except Exception as e:
        print(f"Não foi possível iniciar a interface moderna: {e}")
