"""
Conversor CapCut — Interface Web (HTML/CSS)
─────────────────────────────────────────────
Este arquivo NÃO reimplementa a lógica de conversão: ele reaproveita
100% do converter.py já testado (build_draft, load_config, save_config,
find_capcut_drafts, get_audio_duration_us, build_meta_info, etc.) e só
troca a "casca" visual — de Tkinter para uma janela HTML/CSS (via
pywebview), no mesmo estilo do app de TTS "Crônicas Estelares".

Requisitos:
    pip install pywebview

Arquivos necessários na mesma pasta:
    converter.py            (motor de conversão)
    capcut_templates.json   (moldes de efeitos: zoom, transição, VHS, marca)
    gui.html                (interface visual)
"""
import os
import time
import json
import shutil
import traceback
import subprocess
import platform
from pathlib import Path

try:
    import webview
    HAS_WEBVIEW = True
except ImportError:
    HAS_WEBVIEW = False
    webview = None

import converter as core   # reaproveita toda a lógica já validada


def select_file(title="Selecionar arquivo", file_types=(), linux_filter=""):
    """Abre seletor de arquivo de forma cross-platform e resiliente."""
    if platform.system() == "Linux" and shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", f"--title={title}"]
        if linux_filter:
            cmd.append(f"--file-filter={linux_filter}")
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            if res.returncode == 0:
                return res.stdout.strip()
            return ""
        except Exception as e:
            print(f"[zenity error] {e}")

    try:
        if getattr(webview, "windows", None):
            res = webview.windows[0].create_file_dialog(webview.OPEN_DIALOG, file_types=file_types)
            if res:
                return res[0]
    except Exception as e:
        print(f"[create_file_dialog error] {e}")
    return ""


def select_folder(title="Selecionar pasta"):
    """Abre seletor de pasta de forma cross-platform e resiliente."""
    if platform.system() == "Linux" and shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--directory", f"--title={title}"]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            if res.returncode == 0:
                return res.stdout.strip()
            return ""
        except Exception as e:
            print(f"[zenity error] {e}")

    try:
        if getattr(webview, "windows", None):
            res = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            if res:
                return res[0]
    except Exception as e:
        print(f"[create_file_dialog error] {e}")
    return ""


class Api:
    def _parse_json_meta(self, path):
        if not path or not os.path.isfile(path):
            return {}
        p = Path(path)
        meta = {
            "path": str(p).replace("\\", "/"),
            "auto_proj_name": p.stem
        }
        # Auto-preenchimento inteligente da pasta de mídias
        p_parent = p.parent
        valid_media_exts = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".avi", ".mkv", ".webm"}
        try:
            has_media = any(f.suffix.lower() in valid_media_exts for f in p_parent.iterdir() if f.is_file())
            if has_media:
                meta["auto_media_folder"] = str(p_parent).replace("\\", "/")
        except Exception:
            pass

        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if "fps" in d:
                meta["fps"] = str(d["fps"])
            if "formato" in d:
                fmt = str(d["formato"]).strip()
                if fmt == "16:9":
                    meta["resolution"] = "1920x1080"
                elif fmt == "9:16":
                    meta["resolution"] = "1080x1920"
                elif fmt == "1:1":
                    meta["resolution"] = "1080x1080"
            library = d.get("library", d.get("mediaItems", []))
            lib_map = {m["id"]: m for m in library if isinstance(m, dict) and "id" in m}
            audio_names = []
            for c in d.get("clips", []):
                if str(c.get("trackId", "")).lower().startswith("audio"):
                    m_item = lib_map.get(c.get("mediaId"))
                    if m_item and "name" in m_item:
                        audio_names.append({"name": m_item["name"], "volume": float(c.get("volume", 1.0))})
            if audio_names:
                meta["audio_files"] = audio_names
        except Exception:
            pass
        return meta

    # ── Diálogos de arquivo / pasta ────────────────────────────────────────
    def pick_json(self):
        path = select_file(
            title="Selecione o arquivo JSON do projeto",
            file_types=("Arquivos JSON (*.json)", "Todos os arquivos (*.*)"),
            linux_filter="Arquivos JSON (*.json) | *.json *.JSON"
        )
        if not path:
            return {}
        return self._parse_json_meta(path)

    def handle_drop_path(self, raw_path):
        """Trata arquivos ou pastas arrastados e soltos na janela do app."""
        if not raw_path:
            return {"type": "none"}
        p = Path(raw_path.strip().strip('"').strip("'"))
        if not p.exists():
            return {"type": "none"}

        if p.is_file():
            if p.suffix.lower() == ".json":
                meta = self._parse_json_meta(str(p))
                return {"type": "json", "data": meta}
            elif p.suffix.lower() in {".mp3", ".wav"}:
                return {"type": "audio", "name": p.name}
            elif p.suffix.lower() in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
                return {"type": "video", "path": str(p).replace("\\", "/")}
            else:
                return {"type": "file", "path": str(p).replace("\\", "/")}
        elif p.is_dir():
            if (p / "draft_content.json").is_file():
                return {"type": "draft", "path": str(p).replace("\\", "/")}
            else:
                return {"type": "folder", "path": str(p).replace("\\", "/")}
        return {"type": "none"}

    def pick_media_folder(self):
        return select_folder(title="Selecione a pasta das mídias")

    def pick_drafts_folder(self):
        folder = select_folder(title="Selecione a pasta de drafts do CapCut")
        if folder and os.path.isdir(folder):
            core.set_drafts_dir(folder)
        return folder

    def set_drafts_dir(self, path):
        try:
            return core.set_drafts_dir(path)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def pick_audio_file(self):
        res = select_file(
            title="Selecione o arquivo de áudio",
            file_types=("Áudio (*.mp3;*.wav;*.MP3;*.WAV)", "Todos os arquivos (*.*)"),
            linux_filter="Áudio (*.mp3;*.wav) | *.mp3 *.MP3 *.wav *.WAV"
        )
        return Path(res).name if res else ""

    def get_default_drafts(self):
        try:
            return core.find_capcut_drafts()
        except Exception:
            return ""

    # ── Compactador de Áudios de TTS ─────────────────────────────────────────
    def list_drafts(self, drafts_dir=""):
        try:
            return core.list_capcut_drafts(drafts_dir)
        except Exception as e:
            print(f"[list_drafts error] {e}")
            return []

    def inspect_draft(self, draft_folder):
        try:
            return core.inspect_draft_audios(draft_folder)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def compact_audios(self, payload):
        try:
            draft_folder = (payload.get("draft_folder") or "").strip()
            gap_seconds = float(payload.get("gap_seconds", 0.0))
            start_offset = float(payload.get("start_offset", 0.0))
            sync_subtitles = bool(payload.get("sync_subtitles", True))
            track_index = int(payload.get("track_index", -1))
            trim_silence = bool(payload.get("trim_silence", False))
            trim_start_s = float(payload.get("trim_start_s", 0.10))
            trim_end_s = float(payload.get("trim_end_s", 0.15))
            micro_fade = bool(payload.get("micro_fade", False))
            micro_fade_ms = float(payload.get("micro_fade_ms", 15.0))

            if not draft_folder or not os.path.isdir(draft_folder):
                return {"ok": False, "error": "Pasta do projeto não encontrada."}

            return core.compact_draft_audios(
                draft_folder=draft_folder,
                gap_seconds=gap_seconds,
                start_offset=start_offset,
                sync_subtitles=sync_subtitles,
                track_index=track_index,
                trim_silence=trim_silence,
                trim_start_s=trim_start_s,
                trim_end_s=trim_end_s,
                micro_fade=micro_fade,
                micro_fade_ms=micro_fade_ms
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def restore_backup(self, draft_folder):
        try:
            return core.restore_draft_backup(draft_folder)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def launch_capcut(self):
        try:
            res = core.launch_capcut()
            if not res.get("ok") and res.get("need_path"):
                chosen = select_file(
                    title="Selecione o executável do CapCut (CapCut.exe)",
                    file_types=("Executáveis (*.exe;*.lnk)", "Todos os arquivos (*.*)"),
                    linux_filter="Executáveis (*.exe) | *.exe *.lnk"
                )
                if chosen:
                    save_res = core.set_capcut_path(chosen)
                    if save_res.get("ok"):
                        return core.launch_capcut()
                    return save_res
                return res
            return res
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_capcut_path(self, path):
        try:
            return core.set_capcut_path(path)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Extração e Concatenação de Áudio (Aba 3) ─────────────────────────────
    def pick_video_file(self):
        return select_file(
            title="Selecione o arquivo de vídeo (.mp4)",
            file_types=("Vídeos (*.mp4;*.mov;*.avi;*.mkv;*.webm)", "Todos os arquivos (*.*)"),
            linux_filter="Vídeos (*.mp4;*.mov) | *.mp4 *.MP4 *.mov *.MOV"
        )

    def pick_audio_dest_folder(self):
        return select_folder(title="Selecione a pasta de destino do áudio")

    def inspect_draft_extraction(self, draft_folder):
        try:
            return core.inspect_draft_for_audio_extraction(draft_folder)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def extract_video_audio(self, payload):
        try:
            video_path = (payload.get("video_path") or "").strip()
            output_path = (payload.get("output_path") or "").strip()
            audio_format = payload.get("format", "mp3")
            bitrate = payload.get("bitrate", "320k")
            return core.extract_audio_from_video(video_path, output_path, audio_format, bitrate)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def stitch_tts_audio(self, payload):
        try:
            draft_folder = (payload.get("draft_folder") or "").strip()
            output_path = (payload.get("output_path") or "").strip()
            audio_format = payload.get("format", "mp3")
            bitrate = payload.get("bitrate", "320k")
            gap_seconds = float(payload.get("gap_seconds", 0.0))
            trim_silence = bool(payload.get("trim_silence", False))
            trim_start_s = float(payload.get("trim_start_s", 0.10))
            trim_end_s = float(payload.get("trim_end_s", 0.15))

            return core.stitch_tts_from_draft(
                draft_folder=draft_folder,
                output_path=output_path,
                audio_format=audio_format,
                bitrate=bitrate,
                gap_seconds=gap_seconds,
                trim_silence=trim_silence,
                trim_start_s=trim_start_s,
                trim_end_s=trim_end_s
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    # ── Juntador de Pasta de Áudios ou Vídeos (CapCut Online / TTS) ─────────
    def pick_folder_audios(self):
        return select_folder(title="Selecione a pasta com os arquivos de áudio ou vídeo (.mp4)")

    def pick_multiple_audios(self):
        file_types = (
            "Vídeos e Áudios (*.mp4;*.mov;*.mkv;*.webm;*.mp3;*.wav;*.m4a;*.aac;*.ogg;*.flac)",
            "Vídeos (*.mp4;*.mov;*.mkv;*.webm)",
            "Áudios (*.mp3;*.wav;*.m4a;*.aac;*.ogg;*.flac;*.wma)",
            "Todos os arquivos (*.*)"
        )
        try:
            if getattr(webview, "windows", None):
                res = webview.windows[0].create_file_dialog(
                    webview.OPEN_DIALOG,
                    allow_multiple=True,
                    file_types=file_types
                )
                if res:
                    return list(res)
        except Exception as e:
            print(f"[pick_multiple_audios error] {e}")
        return []

    def inspect_audio_folder(self, folder_path):
        try:
            return core.inspect_audio_folder(folder_path)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def stitch_audio_folder(self, payload):
        try:
            folder_path = (payload.get("folder_path") or "").strip()
            file_paths = payload.get("file_paths") or []
            output_path = (payload.get("output_path") or "").strip()
            audio_format = payload.get("format", "mp3")
            bitrate = payload.get("bitrate", "320k")
            gap_seconds = float(payload.get("gap_seconds", 0.0))
            trim_silence = bool(payload.get("trim_silence", False))
            trim_start_s = float(payload.get("trim_start_s", 0.10))
            trim_end_s = float(payload.get("trim_end_s", 0.15))
            reverse_order = bool(payload.get("reverse_order", False))

            return core.stitch_audio_folder(
                folder_path=folder_path,
                file_paths=file_paths,
                output_path=output_path,
                audio_format=audio_format,
                bitrate=bitrate,
                gap_seconds=gap_seconds,
                trim_silence=trim_silence,
                trim_start_s=trim_start_s,
                trim_end_s=trim_end_s,
                reverse_order=reverse_order
            )
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def open_file(self, path):
        try:
            if not path or not os.path.exists(path):
                return "Arquivo não encontrado"
            if platform.system() == "Windows":
                os.startfile(path)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            return ""
        except Exception as e:
            return str(e)

    def open_folder(self, path):
        try:
            if not path or not os.path.exists(path):
                return "Caminho não encontrado"
            system = platform.system()
            if system == "Windows":
                os.startfile(path)
            elif system == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            return str(e)
        return ""

    # ── Configuração persistente (mesmo arquivo do app Tkinter) ────────────
    def load_config(self):
        try:
            cfg = core.load_config()
            drafts_dir = cfg.get("drafts_dir", "")
            # Se a pasta salva não existir nesta máquina, tenta auto-detectar
            if not drafts_dir or not os.path.exists(drafts_dir):
                detected = core.find_capcut_drafts()
                if detected and os.path.exists(detected):
                    cfg["drafts_dir"] = detected
                    core.save_config(cfg)
                else:
                    cfg["drafts_dir"] = ""
            return cfg
        except Exception:
            return {}

    def save_config(self, cfg):
        try:
            core.save_config(cfg)
        except Exception:
            pass
        return True

    # ── Validação (equivalente ao botão "Validar JSON" do app original) ───
    def validate(self, payload):
        logs = []
        json_path    = (payload.get("json_path") or "").strip()
        media_folder = (payload.get("media_folder") or "").strip()
        img_ext      = payload.get("img_ext", "jpg")
        audio_files  = payload.get("audio_files", [])

        if not json_path or not os.path.isfile(json_path):
            logs.append("❌ JSON não encontrado.")
            return logs
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logs.append(f"❌ JSON inválido: {e}")
            return logs

        clips = data.get("clips", [])
        valid_clips = [c for c in clips if c.get("duration", 0) > 0]

        video_clips = [c for c in valid_clips if not str(c.get("trackId", "")).lower().startswith("audio")]
        audio_clips = [c for c in valid_clips if str(c.get("trackId", "")).lower().startswith("audio")]

        v_dur = sum(c["duration"] for c in video_clips)
        vm, vs = divmod(int(v_dur), 60)
        logs.append(f"✅ {len(video_clips)} cenas de vídeo/imagem ({vm}m{vs:02d}s) | {len(audio_clips)} faixas de áudio")

        if "formato" in data or "fps" in data:
            logs.append(f"📺 Formato: {data.get('formato', '16:9')} | FPS: {data.get('fps', 24)} | Qualidade: {data.get('qualidade', '1080p')}")

        media_items = data.get("mediaItems", data.get("media", data.get("library", [])))
        media_map = {m["id"]: m for m in media_items} if media_items else {}

        if media_folder and os.path.isdir(media_folder):
            missing_imgs, found_imgs = [], []
            missing_vids, found_vids = [], []
            VIDEO_EXTS_L = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}

            for idx, c in enumerate(video_clips, start=1):
                mid = c.get("mediaId", "")
                m_item = media_map.get(mid)
                dur_info = f"{c.get('duration', 0)}s"
                if m_item:
                    name = m_item.get("name", "")
                    ext = Path(name).suffix.lower()
                    if ext in VIDEO_EXTS_L or str(m_item.get("type", "")).startswith("video/"):
                        p = Path(media_folder) / name
                        if p.exists():
                            found_vids.append(name)
                        else:
                            missing_vids.append(f"Cena {idx} ({dur_info}): vídeo '{name}'")
                    else:
                        img_name = Path(name).stem + f".{img_ext}"
                        p = Path(media_folder) / img_name
                        if p.exists():
                            found_imgs.append(img_name)
                        else:
                            missing_imgs.append(f"Cena {idx} ({dur_info}): imagem '{img_name}'")
                else:
                    missing_imgs.append(f"Cena {idx} ({dur_info}): mídia ID '{mid}' não declarada no projeto")

            if missing_imgs:
                logs.append(f"❌ Imagens ausentes em {len(missing_imgs)} cena(s):")
                for item in missing_imgs[:8]:
                    logs.append(f"   ✗ {item}")
                if len(missing_imgs) > 8:
                    logs.append(f"   ... e mais {len(missing_imgs)-8} cenas")
            elif found_imgs:
                logs.append(f"🖼️ Imagens: todas as {len(found_imgs)} encontradas com sucesso")

            if missing_vids:
                logs.append(f"❌ Vídeos ausentes em {len(missing_vids)} cena(s):")
                for item in missing_vids[:8]:
                    logs.append(f"   ✗ {item}")
                if len(missing_vids) > 8:
                    logs.append(f"   ... e mais {len(missing_vids)-8} cenas")
            elif found_vids:
                logs.append(f"🎬 Vídeos MP4: todos os {len(found_vids)} encontrados com sucesso")

            all_audios = list(audio_files or [])
            for ac in audio_clips:
                m_item = media_map.get(ac.get("mediaId", ""))
                if m_item and "name" in m_item:
                    aname = m_item["name"]
                    if not any(a.get("name", "").lower() == aname.lower() for a in all_audios):
                        all_audios.append({"name": aname})

            for ainfo in all_audios:
                apath = Path(media_folder) / ainfo["name"]
                status = "✅" if apath.exists() else "❌"
                dur = core.get_audio_duration_us(str(apath))
                dur_str = f" — {dur//1_000_000}s" if dur else ""
                logs.append(f"🎵 {status} {ainfo['name']}{dur_str}")
        else:
            logs.append("⚠️ Informe a pasta de mídias para verificar os arquivos.")

        return logs

    # ── Conversão (equivalente ao botão "Gerar Draft") ─────────────────────
    def convert(self, payload):
        try:
            json_path    = (payload.get("json_path") or "").strip()
            media_folder = (payload.get("media_folder") or "").strip()
            proj_name    = (payload.get("proj_name") or "").strip() or "MeuProjeto"
            drafts_dir   = (payload.get("drafts_dir") or "").strip()
            resolution   = payload.get("resolution", "1920x1080")
            fps          = int(payload.get("fps", 60))
            img_ext      = payload.get("img_ext", "jpg")
            audio_files  = payload.get("audio_files", [])

            apply_zoom       = bool(payload.get("apply_zoom", True))
            apply_transition = bool(payload.get("apply_transition", True))
            apply_vhs        = bool(payload.get("apply_vhs", True))
            apply_watermark  = bool(payload.get("apply_watermark", True))
            zoom_threshold      = float(payload.get("zoom_threshold", 5.0))
            transition_duration = float(payload.get("transition_dur", 0.4667))
            watermark_text      = (payload.get("watermark_text") or "").strip()

            if not json_path or not os.path.isfile(json_path):
                return {"ok": False, "error": "JSON não encontrado."}
            if not drafts_dir:
                return {"ok": False, "error": "Informe a pasta de drafts do CapCut."}

            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            clips = data.get("clips", [])
            media_items = data.get("mediaItems", data.get("media", data.get("library", [])))

            def progress_cb(cur, total):
                if total > 0 and getattr(webview, "windows", None):
                    pct = round((cur / total) * 100)
                    try:
                        webview.windows[0].evaluate_js(f"window.updateProgress && window.updateProgress({pct});")
                    except Exception:
                        pass

            draft = core.build_draft(
                proj_name, clips, media_items, media_folder, audio_files,
                resolution, fps, img_ext,
                progress_cb=progress_cb,
                apply_zoom=apply_zoom, apply_transition=apply_transition,
                apply_vhs=apply_vhs, apply_watermark=apply_watermark,
                zoom_threshold=zoom_threshold, transition_duration=transition_duration,
                watermark_text=watermark_text,
            )

            dur_s = draft["duration"] / 1_000_000
            logs = [f"✅ Draft gerado — {int(dur_s//60)}m{int(dur_s%60):02d}s"]

            safe_name = "".join(
                c for c in proj_name if c.isalnum() or c in " _-"
            ).strip() or "MeuProjeto"
            dest = os.path.join(drafts_dir, safe_name)
            os.makedirs(dest, exist_ok=True)

            with open(os.path.join(dest, "draft_content.json"), "w", encoding="utf-8") as f:
                json.dump(draft, f, ensure_ascii=False, indent=2)

            meta = core.build_meta_info(proj_name, draft["duration"], dest, drafts_dir, draft)
            with open(os.path.join(dest, "draft_meta_info.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

            logs.append("🎬 Abra o CapCut e procure na lista de drafts.")

            core.save_config(payload)

            return {"ok": True, "dest": dest, "logs": logs}

        except Exception as e:
            tb = traceback.format_exc()
            print(tb)
            return {"ok": False, "error": str(e)}


def show_native_error(title, message):
    """Exibe erro nativo em pop-up para evitar que o console feche em silêncio."""
    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, str(message), str(title), 0x10)
            return
        except Exception:
            pass
    print(f"[{title}] {message}")


def main():
    global webview, HAS_WEBVIEW
    if not HAS_WEBVIEW:
        should_install = False
        if platform.system() == "Windows":
            try:
                import ctypes
                msg = (
                    "A biblioteca 'pywebview' necessária para a interface visual não foi encontrada.\n\n"
                    "Deseja tentar instalá-la automaticamente agora?"
                )
                res = ctypes.windll.user32.MessageBoxW(0, msg, "Conversor CapCut — Dependência Ausente", 0x24)
                should_install = (res == 6)
            except Exception:
                pass
        if should_install:
            try:
                import sys
                subprocess.check_call([sys.executable, "-m", "pip", "install", "pywebview"])
                import webview as _wv
                webview = _wv
                HAS_WEBVIEW = True
            except Exception as e:
                show_native_error("Erro de Instalação", f"Falha ao instalar pywebview:\n{e}")
                return
        else:
            show_native_error(
                "Conversor CapCut — Dependência Ausente",
                "A biblioteca 'pywebview' não está instalada.\n"
                "Instale executando no terminal: pip install pywebview\n"
                "Consulte o arquivo 'como instalar.txt' para mais detalhes."
            )
            return

    try:
        api = Api()
        html_path = Path(__file__).parent / "gui.html"
        if not html_path.exists():
            show_native_error("Erro", f"Arquivo 'gui.html' não encontrado em:\n{html_path}")
            return
        webview.create_window(
            "Conversor CapCut",
            str(html_path),
            js_api=api,
            width=980,
            height=660,
            min_size=(760, 520),
        )
        webview.start(private_mode=True)
    except Exception as e:
        show_native_error("Erro ao iniciar", f"Ocorreu um erro ao abrir a interface:\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()
