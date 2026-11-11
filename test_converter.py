"""
Testes automatizados para o Conversor CapCut
"""
import os
import json
import wave
import struct
import tempfile
import shutil
import subprocess
import unittest
from unittest import mock
from pathlib import Path

import converter as core
import converter_gui


class TestAudioDuration(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_wav_duration_standard(self):
        wav_path = os.path.join(self.test_dir, "test.wav")
        # Cria um WAV de 2 segundos, 44100Hz, estéreo, 16 bits
        sample_rate = 44100
        duration_s = 2.0
        n_frames = int(sample_rate * duration_s)
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(b"\x00\x00" * 2 * n_frames)

        dur_us = core.get_audio_duration_us(wav_path)
        self.assertIsNotNone(dur_us)
        # 2 segundos = 2_000_000 microssegundos
        self.assertEqual(dur_us, 2_000_000)

    def test_wav_duration_extended_fmt(self):
        # Cria manualmente um WAV com chunk 'fmt ' maior que 16 bytes (ex: 18 bytes)
        wav_path = os.path.join(self.test_dir, "extended.wav")
        sample_rate = 48000
        n_channels = 2
        bits_per_sample = 16
        block_align = n_channels * (bits_per_sample // 8)
        byte_rate = sample_rate * block_align
        num_samples = 48000  # 1 segundo
        data_size = num_samples * block_align
        fmt_size = 18

        with open(wav_path, "wb") as f:
            f.write(b"RIFF")
            f.write(struct.pack("<I", 36 + fmt_size - 16 + data_size))
            f.write(b"WAVE")
            f.write(b"fmt ")
            f.write(struct.pack("<I", fmt_size))
            f.write(struct.pack("<HHIIHH", 1, n_channels, sample_rate, byte_rate, block_align, bits_per_sample))
            f.write(b"\x00\x00")  # 2 bytes extras do fmt
            f.write(b"data")
            f.write(struct.pack("<I", data_size))
            f.write(b"\x00" * data_size)

        dur_us = core.get_audio_duration_us(wav_path)
        self.assertIsNotNone(dur_us)
        self.assertEqual(dur_us, 1_000_000)


    def test_mp3_duration_exact_via_ffmpeg(self):
        ffmpeg = core.get_ffmpeg_bin()
        if not ffmpeg:
            self.skipTest("ffmpeg não disponível para teste de MP3")

        mp3_path = os.path.join(self.test_dir, "test_sine.mp3")
        # Gera 3.5 segundos de áudio codificado em MP3
        subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=3.5", "-c:a", "libmp3lame", "-b:a", "192k", mp3_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )

        dur_us = core.get_audio_duration_us(mp3_path)
        self.assertIsNotNone(dur_us)
        # 3.5 segundos = 3_500_000 microssegundos (tolerância de até 50ms devido a frames de padding do encoder MP3)
        self.assertAlmostEqual(dur_us, 3_500_000, delta=50_000)


class TestFitScale(unittest.TestCase):
    def test_contain(self):
        sx, sy, tx, ty = core.compute_fit_scale("contain", 1920, 1080, 1920, 1080)
        self.assertEqual((sx, sy, tx, ty), (1.0, 1.0, 0.0, 0.0))

    def test_cover(self):
        sx, sy, tx, ty = core.compute_fit_scale("cover", 1920, 1080, 1920, 1080)
        self.assertEqual((sx, sy, tx, ty), (1.0, 1.0, 0.0, 0.0))


class TestDraftBuilding(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_auto_detect_real_dimensions_when_missing_in_json(self):
        from PIL import Image
        img_name = "foto_vertical.png"
        img_path = os.path.join(self.test_dir, img_name)
        Image.new("RGB", (1080, 1920), color="red").save(img_path)

        clips = [{"id": "c1", "mediaId": "m1", "duration": 3.0, "fit": "contain"}]
        # Sem width e sem height no JSON:
        media_items = [{"id": "m1", "name": img_name, "type": "image/png"}]

        draft = core.build_draft(
            project_name="TesteMedidasReais",
            clips=clips,
            media_items=media_items,
            media_folder=self.test_dir,
            audio_files=[],
            resolution="1920x1080",
            fps=30,
            img_ext="png"
        )

        mat = draft["materials"]["videos"][0]
        self.assertEqual(mat["width"], 1080)
        self.assertEqual(mat["height"], 1920)

        seg = draft["tracks"][0]["segments"][0]
        scale_x = seg["clip"]["scale"]["x"]
        scale_y = seg["clip"]["scale"]["y"]
        self.assertAlmostEqual(scale_x, 0.31640625, places=4)
        self.assertEqual(scale_y, 1.0)

    def test_build_draft_without_media_items(self):
        # Cenário que antes falhava gerando 0 cenas
        clips = [
            {"id": "c1", "mediaId": "m1", "duration": 3.5},
            {"id": "c2", "mediaId": "m2", "duration": 4.0},
        ]
        draft = core.build_draft(
            project_name="TesteSemMedia",
            clips=clips,
            media_items=[],
            media_folder=self.test_dir,
            audio_files=[],
            resolution="1920x1080",
            fps=60,
            img_ext="jpg"
        )
        video_track = [t for t in draft["tracks"] if t["type"] == "video"][0]
        self.assertEqual(len(video_track["segments"]), 2)
        # Confirma que os nomes sintéticos gerados usaram a extensão correta
        materials = draft["materials"]["videos"]
        self.assertEqual(len(materials), 2)
        self.assertTrue(materials[0]["material_name"].endswith(".jpg"))
        self.assertTrue(materials[1]["material_name"].endswith(".jpg"))

    def test_build_draft_with_ext_conversion(self):
        # Cenário onde o JSON tem .png mas o usuário selecionou .jpg
        clips = [{"id": "c1", "mediaId": "m1", "duration": 5.0}]
        media_items = [{"id": "m1", "name": "foto_original.PNG", "type": "image/png"}]
        draft = core.build_draft(
            project_name="TesteExt",
            clips=clips,
            media_items=media_items,
            media_folder=self.test_dir,
            audio_files=[],
            resolution="1920x1080",
            fps=60,
            img_ext="jpg"
        )
        mat = draft["materials"]["videos"][0]
        self.assertEqual(mat["material_name"], "foto_original.jpg")
        self.assertTrue(mat["path"].endswith("foto_original.jpg"))

    def test_build_draft_preserves_clip_start_time_and_gaps(self):
        clips = [
            {"id": "c1", "mediaId": "m1", "startTime": 0.0, "duration": 3.0},
            {"id": "c2", "mediaId": "m2", "startTime": 5.0, "duration": 4.0},
        ]
        media_items = [
            {"id": "m1", "name": "c1.jpg", "type": "image/jpeg"},
            {"id": "m2", "name": "c2.jpg", "type": "image/jpeg"},
        ]
        draft = core.build_draft(
            project_name="TesteGaps",
            clips=clips,
            media_items=media_items,
            media_folder=self.test_dir,
            audio_files=[],
            resolution="1920x1080",
            fps=30,
            img_ext="jpg"
        )
        segs = draft["tracks"][0]["segments"]
        self.assertEqual(segs[0]["target_timerange"]["start"], 0)
        self.assertEqual(segs[0]["target_timerange"]["duration"], 3_000_000)
        # Início aos 5s respeitado (não comprimido para 3s)
        self.assertEqual(segs[1]["target_timerange"]["start"], 5_000_000)
        self.assertEqual(segs[1]["target_timerange"]["duration"], 4_000_000)
        self.assertEqual(draft["duration"], 9_000_000)

    def test_build_draft_stops_with_clear_error_on_missing_media(self):
        # Clip intermediário c2 sem media correspondente deve parar com erro claro listando a cena
        clips = [
            {"id": "c1", "mediaId": "m1", "duration": 2.0},
            {"id": "c2", "mediaId": "m_inexistente", "duration": 5.0},
            {"id": "c3", "mediaId": "m3", "duration": 3.0},
        ]
        media_items = [
            {"id": "m1", "name": "f1.jpg", "type": "image/jpeg"},
            {"id": "m3", "name": "f3.jpg", "type": "image/jpeg"},
        ]
        # 1. Por padrão, interrompe com ValueError indicando a cena afetada para não quebrar áudio
        with self.assertRaises(ValueError) as ctx:
            core.build_draft(
                project_name="TesteSemGaps",
                clips=clips,
                media_items=media_items,
                media_folder=self.test_dir,
                audio_files=[],
                resolution="1920x1080",
                fps=60,
                img_ext="jpg"
            )
        self.assertIn("Cena 2", str(ctx.exception))
        self.assertIn("m_inexistente", str(ctx.exception))

        # 2. Se for permitido prosseguir (allow_missing=True), mantém o espaço da cena para não dessincronizar áudio
        draft = core.build_draft(
            project_name="TesteSemGaps",
            clips=clips,
            media_items=media_items,
            media_folder=self.test_dir,
            audio_files=[],
            resolution="1920x1080",
            fps=60,
            img_ext="jpg",
            allow_missing=True
        )
        video_track = [t for t in draft["tracks"] if t["type"] == "video"][0]
        segs = video_track["segments"]
        self.assertEqual(len(segs), 3)
        self.assertEqual(segs[0]["target_timerange"]["start"], 0)
        self.assertEqual(segs[0]["target_timerange"]["duration"], 2_000_000)
        self.assertEqual(segs[1]["target_timerange"]["start"], 2_000_000)
        self.assertEqual(segs[1]["target_timerange"]["duration"], 5_000_000)
        self.assertEqual(segs[2]["target_timerange"]["start"], 7_000_000)
        self.assertEqual(segs[2]["target_timerange"]["duration"], 3_000_000)
        self.assertEqual(draft["duration"], 10_000_000)

    def test_full_conversion_api(self):
        # Preserva o config original do usuário
        original_cfg = core.load_config()
        try:
            # Testa a API de converter_gui
            api = converter_gui.Api()

            # Cria arquivos fictícios para teste
            json_file = os.path.join(self.test_dir, "projeto.json")
            img_file = os.path.join(self.test_dir, "cena1.jpg")
            with open(img_file, "wb") as f:
                f.write(b"fake image data")

            data = {
                "clips": [{"id": "c1", "mediaId": "m1", "duration": 4.0}],
                "mediaItems": [{"id": "m1", "name": "cena1.jpg", "type": "image/jpeg"}]
            }
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(data, f)

            drafts_out = os.path.join(self.test_dir, "drafts_capcut")
            payload = {
                "json_path": json_file,
                "media_folder": self.test_dir,
                "proj_name": "ProjetoTesteFinal",
                "drafts_dir": drafts_out,
                "resolution": "1920x1080",
                "fps": 60,
                "img_ext": "jpg",
                "audio_files": [],
                "apply_zoom": True,
                "apply_transition": True,
                "apply_vhs": True,
                "apply_watermark": True,
                "zoom_threshold": 5.0,
                "transition_dur": 0.5,
                "watermark_text": "Marca Teste"
            }

            # Validação
            logs_val = api.validate(payload)
            self.assertTrue(any("✅" in l for l in logs_val))

            # Conversão
            res = api.convert(payload)
            self.assertTrue(res["ok"])
            self.assertTrue(os.path.isdir(res["dest"]))
            self.assertTrue(os.path.isfile(os.path.join(res["dest"], "draft_content.json")))
            self.assertTrue(os.path.isfile(os.path.join(res["dest"], "draft_meta_info.json")))
        finally:
            core.save_config(original_cfg)

    def test_effects_zoom_threshold_and_layers(self):
        # Clip curto (2s <= threshold 5s) usa Zoom 1, clip longo (6s > threshold 5s) usa Quicar 1
        clips = [
            {"id": "c1", "mediaId": "m1", "duration": 2.0},
            {"id": "c2", "mediaId": "m2", "duration": 6.0},
        ]
        media_items = [
            {"id": "m1", "name": "c1.jpg"},
            {"id": "m2", "name": "c2.jpg"},
        ]
        draft = core.build_draft(
            project_name="TesteEfeitos",
            clips=clips,
            media_items=media_items,
            media_folder=self.test_dir,
            audio_files=[{"name": "audio.mp3", "volume": 1.5}],
            resolution="1920x1080",
            fps=30,
            img_ext="jpg",
            apply_zoom=True,
            apply_transition=True,
            apply_vhs=True,
            apply_watermark=True,
            zoom_threshold=5.0,
            watermark_text="Canal Teste"
        )
        # Confirma presença de animações, transições e tracks adicionais
        materials = draft["materials"]
        self.assertTrue(len(materials["material_animations"]) >= 2)
        # Transição deve existir entre cena 1 e 2 (1 transição para 2 cenas)
        self.assertEqual(len(materials["transitions"]), 1)
        # Filtro VHS e marca d'água geram tracks extras além do vídeo e áudio
        tracks = draft["tracks"]
        track_types = [t["type"] for t in tracks]
        self.assertIn("video", track_types)
        self.assertIn("audio", track_types)
        self.assertIn("effect", track_types)
        self.assertIn("text", track_types)

    def test_per_clip_transitions_animations_and_fades(self):
        clips = [
            {
                "id": "c1", "mediaId": "m1", "duration": 2.0,
                "transition": "fade_black", "transitionDuration": 1.2,
                "animation": "quicar"
            },
            {
                "id": "c2", "mediaId": "m2", "duration": 4.0,
                "transition": "disabled",
                "fadeIn": 0.5, "fadeOut": 0.8
            },
            {
                "id": "c3", "mediaId": "m3", "duration": 3.0,
                "animation": "none"
            }
        ]
        media_items = [
            {"id": "m1", "name": "f1.jpg"},
            {"id": "m2", "name": "f2.jpg"},
            {"id": "m3", "name": "f3.jpg"},
        ]
        draft = core.build_draft(
            project_name="TestePerClipFx",
            clips=clips,
            media_items=media_items,
            media_folder=self.test_dir,
            audio_files=[],
            resolution="1920x1080",
            fps=30,
            img_ext="jpg",
            apply_zoom=True,
            apply_transition=False,  # Globalmente desligado, mas clipe 1 tem transição própria
            apply_watermark=False
        )
        materials = draft["materials"]
        # Transições: apenas o clipe 1 tem transição com 1.2s (1_200_000 us)
        self.assertEqual(len(materials["transitions"]), 1)
        self.assertEqual(materials["transitions"][0]["duration"], 1_200_000)

        # Animações: clipe 1 forçou 'quicar' mesmo com 2s; clipe 3 forçou 'none'
        anim_names = [a["animations"][0]["name"] for a in materials["material_animations"]]
        self.assertIn("Quicar 1", anim_names)
        self.assertEqual(len(materials["material_animations"]), 2)

        # Fades: clipe 2 declarou fadeIn 0.5s e fadeOut 0.8s
        self.assertEqual(len(materials["audio_fades"]), 1)
        self.assertEqual(materials["audio_fades"][0]["fade_in_duration"], 500_000)
        self.assertEqual(materials["audio_fades"][0]["fade_out_duration"], 800_000)

    def test_build_meta_info(self):
        meta = core.build_meta_info("MeuProjeto", 10_000_000)
        self.assertEqual(meta["draft_name"], "MeuProjeto")
        self.assertEqual(meta["tm_duration"], 10_000_000)
        self.assertIn("draft_id", meta)

    def test_find_capcut_drafts(self):
        detected = core.find_capcut_drafts()
        self.assertIsInstance(detected, str)
        self.assertTrue(len(detected) > 0)

    def test_new_format_with_mp4_and_embedded_audio(self):
        # Testa projeto_cronicas.json com clipes MP4 e áudio embutido
        json_path = os.path.join(os.path.dirname(__file__), "projeto_cronicas.json")
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        draft = core.build_draft(
            project_name="TesteCronicasMP4",
            clips=data["clips"],
            media_items=data["library"],
            media_folder=self.test_dir,
            audio_files=[],
            resolution="1920x1080",
            fps=24,
            apply_zoom=True,
            apply_watermark=False
        )

        video_track = [t for t in draft["tracks"] if t["type"] == "video"][0]
        # Deve ter exatamente 6 cenas de vídeo (o clipe de áudio NÃO pode estar na trilha de vídeo)
        self.assertEqual(len(video_track["segments"]), 6)

        # Materiais de vídeo devem ser do tipo 'video' e has_audio True
        for m in draft["materials"]["videos"]:
            self.assertEqual(m["type"], "video")
            self.assertTrue(m["has_audio"])

        # Deve ter 1 track de áudio gerada automaticamente pelo áudio embutido
        audio_tracks = [t for t in draft["tracks"] if t["type"] == "audio"]
        self.assertEqual(len(audio_tracks), 1)
        self.assertEqual(len(draft["materials"]["audios"]), 1)
        self.assertEqual(draft["materials"]["audios"][0]["name"], "audio.MP3")

        # Em clipes de vídeo, não deve haver animação zoom artificial
        self.assertEqual(len(draft["materials"]["material_animations"]), 0)

    def test_new_format_with_images_and_embedded_audio(self):
        # Testa projeto_cronicas (1).json com imagens e áudio embutido
        json_path = os.path.join(os.path.dirname(__file__), "projeto_cronicas (1).json")
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        draft = core.build_draft(
            project_name="TesteCronicasImg",
            clips=data["clips"],
            media_items=data["library"],
            media_folder=self.test_dir,
            audio_files=[],
            resolution="1920x1080",
            fps=24,
            apply_zoom=True,
            apply_watermark=False
        )

        video_track = [t for t in draft["tracks"] if t["type"] == "video"][0]
        self.assertEqual(len(video_track["segments"]), 6)

        # Materiais de imagem devem ser do tipo 'photo'
        for m in draft["materials"]["videos"]:
            self.assertEqual(m["type"], "photo")

        # Em fotos com apply_zoom=True, as animações devem ser aplicadas
        self.assertEqual(len(draft["materials"]["material_animations"]), 6)

    def test_slowdown_clip_when_scene_longer_than_video(self):
        # Vídeo original de 10 segundos, cena precisa durar 12 segundos (desaceleração)
        clips = [{"id": "c1", "mediaId": "v1", "duration": 12.0, "offset": 0.0}]
        media_items = [{"id": "v1", "name": "clip.mp4", "type": "video/mp4", "duration": 10.0}]

        draft = core.build_draft(
            project_name="TesteSlowDown",
            clips=clips,
            media_items=media_items,
            media_folder=self.test_dir,
            audio_files=[],
            resolution="1920x1080",
            fps=30
        )

        video_track = [t for t in draft["tracks"] if t["type"] == "video"][0]
        seg = video_track["segments"][0]

        # Target duration deve ser 12s e source duration deve ser 10s
        self.assertEqual(seg["target_timerange"]["duration"], 12_000_000)
        self.assertEqual(seg["source_timerange"]["duration"], 10_000_000)

        # Velocidade calculada deve ser 10 / 12 = ~0.8333
        self.assertAlmostEqual(seg["speed"], 10.0 / 12.0, places=3)

    def test_slowdown_clip_case_c_split_video_and_companion_image(self):
        # Caso C: Cena de 20s para vídeo original de 5s (> 5s * 1.40 = 7s).
        # Deve dividir em Segmento 1 (Vídeo 5s em 1.0x) + Segmento 2 (Foto 15s em 1.0x)
        clips = [{"id": "c1", "mediaId": "v1", "duration": 20.0, "offset": 0.0}]
        media_items = [
            {"id": "v1", "name": "0003.mp4", "type": "video/mp4", "duration": 5.0},
            {"id": "img1", "name": "0003.jpg", "type": "image/jpeg", "duration": 5.0}
        ]

        draft = core.build_draft(
            project_name="TesteCasoCSplit",
            clips=clips,
            media_items=media_items,
            media_folder=self.test_dir,
            audio_files=[],
            resolution="1920x1080",
            fps=30
        )

        video_track = [t for t in draft["tracks"] if t["type"] == "video"][0]
        segs = video_track["segments"]

        # Deve gerar exatamente 2 segmentos contíguos
        self.assertEqual(len(segs), 2)

        # Segmento 1: Vídeo original por 5s em velocidade 1.0x
        seg_vid = segs[0]
        self.assertEqual(seg_vid["material_id"], "v1")
        self.assertEqual(seg_vid["target_timerange"]["start"], 0)
        self.assertEqual(seg_vid["target_timerange"]["duration"], 5_000_000)
        self.assertEqual(seg_vid["source_timerange"]["duration"], 5_000_000)
        self.assertEqual(seg_vid["speed"], 1.0)

        # Segmento 2: Imagem companheira completando os 15s restantes
        seg_img = segs[1]
        self.assertEqual(seg_img["material_id"], "img1")
        self.assertEqual(seg_img["target_timerange"]["start"], 5_000_000)
        self.assertEqual(seg_img["target_timerange"]["duration"], 15_000_000)
        self.assertEqual(seg_img["source_timerange"]["duration"], 15_000_000)
        self.assertEqual(seg_img["speed"], 1.0)

        # A soma total é exatamente 20s da cena (sem dessincronizar áudio)
        total_scene_us = seg_vid["target_timerange"]["duration"] + seg_img["target_timerange"]["duration"]
        self.assertEqual(total_scene_us, 20_000_000)

        # Ambos os materiais estão registrados no projeto
        mat_ids = [m["id"] for m in draft["materials"]["videos"]]
        self.assertIn("v1", mat_ids)
        self.assertIn("img1", mat_ids)

    def test_find_companion_image(self):
        # Testa a busca por imagem companheira nos itens de mídia e pasta
        media_items = [
            {"id": "v1", "name": "cena_01.mp4", "type": "video/mp4"},
            {"id": "p1", "name": "cena_01.jpg", "type": "image/jpeg"},
            {"id": "p2", "name": "outra.png", "type": "image/png"}
        ]
        # 1. Encontra em media_items
        found = core.find_companion_image("cena_01.mp4", media_items, self.test_dir)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], "p1")

        # 2. Quando não há imagem companheira
        not_found = core.find_companion_image("sem_par.mp4", media_items, self.test_dir)
        self.assertIsNone(not_found)

    def test_video_materials_and_meta_info(self):
        clips = [{"id": "c1", "mediaId": "v1", "duration": 5.0, "offset": 0.0}]
        media_items = [{"id": "v1", "name": "video_test.mp4", "type": "video/mp4", "duration": 10.0}]

        draft = core.build_draft(
            project_name="TesteVideoMeta",
            clips=clips,
            media_items=media_items,
            media_folder=self.test_dir,
            audio_files=[{"name": "som.mp3", "volume": 1.0}],
            resolution="1920x1080",
            fps=30
        )

        mat_video = draft["materials"]["videos"][0]
        self.assertEqual(mat_video["type"], "video")
        self.assertEqual(mat_video["extra_type_option"], 0)
        self.assertEqual(mat_video["category_name"], "local")
        self.assertTrue(bool(mat_video["local_material_id"]))

        mat_audio = draft["materials"]["audios"][0]
        self.assertTrue(bool(mat_audio["local_material_id"]))

        meta = core.build_meta_info("TesteVideoMeta", draft["duration"], self.test_dir, self.test_dir, draft)
        self.assertEqual(len(meta["draft_materials"][0]["value"]), 1)
        self.assertEqual(meta["draft_materials"][0]["value"][0]["id"], mat_video["local_material_id"])
        self.assertEqual(meta["draft_materials"][0]["value"][0]["metetype"], "video")
        self.assertEqual(len(meta["draft_materials"][1]["value"]), 1)
        self.assertEqual(meta["draft_materials"][1]["value"][0]["id"], mat_audio["local_material_id"])
        self.assertEqual(meta["draft_materials"][1]["value"][0]["metetype"], "music")

    def test_compact_draft_audios_and_subtitles(self):
        draft_dir = os.path.join(self.test_dir, "draft_tts")
        os.makedirs(draft_dir, exist_ok=True)
        # Mock de um projeto do CapCut com 3 áudios TTS com gaps e 3 legendas
        draft_content = {
            "duration": 20_000_000,
            "tracks": [
                {
                    "type": "audio",
                    "segments": [
                        {"id": "a1", "target_timerange": {"start": 1_000_000, "duration": 2_000_000}},
                        {"id": "a2", "target_timerange": {"start": 6_000_000, "duration": 2_000_000}},
                        {"id": "a3", "target_timerange": {"start": 12_000_000, "duration": 3_000_000}},
                    ]
                },
                {
                    "type": "text",
                    "segments": [
                        {"id": "t1", "target_timerange": {"start": 1_000_000, "duration": 2_000_000}},
                        {"id": "t2", "target_timerange": {"start": 6_000_000, "duration": 2_000_000}},
                        {"id": "t3", "target_timerange": {"start": 12_000_000, "duration": 3_000_000}},
                    ]
                }
            ]
        }
        with open(os.path.join(draft_dir, "draft_content.json"), "w", encoding="utf-8") as f:
            json.dump(draft_content, f)

        res = core.compact_draft_audios(draft_dir, gap_seconds=0.1, start_offset=0.0, sync_subtitles=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["segments_count"], 3)
        self.assertEqual(res["synced_texts"], 3)

        with open(os.path.join(draft_dir, "draft_content.json"), "r", encoding="utf-8") as f:
            updated = json.load(f)

        audio_segs = updated["tracks"][0]["segments"]
        text_segs = updated["tracks"][1]["segments"]

        # Áudio 1: start 0, dur 2s -> fim em 2.0s
        self.assertEqual(audio_segs[0]["target_timerange"]["start"], 0)
        self.assertEqual(audio_segs[0]["target_timerange"]["duration"], 2_000_000)

        # Áudio 2: start 2.0s + 0.1s gap = 2.1s (2_100_000), dur 2s -> fim em 4.1s
        self.assertEqual(audio_segs[1]["target_timerange"]["start"], 2_100_000)
        self.assertEqual(audio_segs[1]["target_timerange"]["duration"], 2_000_000)

        # Áudio 3: start 4.1s + 0.1s gap = 4.2s (4_200_000), dur 3s -> fim em 7.2s
        self.assertEqual(audio_segs[2]["target_timerange"]["start"], 4_200_000)

        # Legendas sincronizadas
        self.assertEqual(text_segs[0]["target_timerange"]["start"], 0)
        self.assertEqual(text_segs[1]["target_timerange"]["start"], 2_100_000)
        self.assertEqual(text_segs[2]["target_timerange"]["start"], 4_200_000)

        # Backup criado
        self.assertTrue(os.path.isfile(os.path.join(draft_dir, "draft_content.json.bak")))

    def test_compact_audios_with_trim_fade_and_restore(self):
        draft_dir = os.path.join(self.test_dir, "draft_trim_fade")
        os.makedirs(draft_dir, exist_ok=True)
        draft_content = {
            "duration": 10_000_000,
            "materials": {},
            "tracks": [
                {
                    "type": "audio",
                    "segments": [
                        {"id": "a1", "target_timerange": {"start": 500_000, "duration": 2_000_000}, "source_timerange": {"start": 0, "duration": 2_000_000}},
                        {"id": "a2", "target_timerange": {"start": 4_000_000, "duration": 2_000_000}, "source_timerange": {"start": 0, "duration": 2_000_000}},
                    ]
                }
            ]
        }
        with open(os.path.join(draft_dir, "draft_content.json"), "w", encoding="utf-8") as f:
            json.dump(draft_content, f)

        res = core.compact_draft_audios(
            draft_dir,
            gap_seconds=0.0,
            start_offset=0.0,
            sync_subtitles=False,
            trim_silence=True,
            trim_start_s=0.10,
            trim_end_s=0.15,
            micro_fade=True,
            micro_fade_ms=15.0
        )
        self.assertTrue(res["ok"])
        self.assertTrue(res["trim_applied"])
        self.assertTrue(res["fade_applied"])

        with open(os.path.join(draft_dir, "draft_content.json"), "r", encoding="utf-8") as f:
            updated = json.load(f)

        segs = updated["tracks"][0]["segments"]
        # Cada clipe tinha 2.0s. Menos 0.10s no início e 0.15s no fim = 1.75s (1_750_000)
        self.assertEqual(segs[0]["target_timerange"]["duration"], 1_750_000)
        self.assertEqual(segs[0]["source_timerange"]["start"], 100_000)
        self.assertEqual(segs[0]["source_timerange"]["duration"], 1_750_000)
        self.assertEqual(segs[0]["target_timerange"]["start"], 0)

        # Segundo clipe colado a 0.0s de gap: start = 1_750_000
        self.assertEqual(segs[1]["target_timerange"]["start"], 1_750_000)
        self.assertEqual(segs[1]["target_timerange"]["duration"], 1_750_000)

        # Micro fade criado em materials
        fades = updated.get("materials", {}).get("audio_fades", [])
        self.assertEqual(len(fades), 2)
        self.assertEqual(fades[0]["fade_in_duration"], 15_000)
        self.assertTrue(len(segs[0].get("extra_material_refs", [])) > 0)

        # Teste de Restauração de Backup
        restore_res = core.restore_draft_backup(draft_dir)
        self.assertTrue(restore_res["ok"])

        with open(os.path.join(draft_dir, "draft_content.json"), "r", encoding="utf-8") as f:
            restored = json.load(f)
        restored_segs = restored["tracks"][0]["segments"]
        self.assertEqual(restored_segs[0]["target_timerange"]["duration"], 2_000_000)
        self.assertEqual(restored_segs[0]["target_timerange"]["start"], 500_000)

    def test_multiple_compaction_preserves_original_backup(self):
        draft_dir = os.path.join(self.test_dir, "draft_multi_compact")
        os.makedirs(draft_dir, exist_ok=True)
        initial_content = {
            "duration": 6_000_000,
            "tracks": [
                {
                    "type": "audio",
                    "segments": [
                        {"id": "seg1", "target_timerange": {"start": 1_000_000, "duration": 2_000_000}, "source_timerange": {"start": 0, "duration": 2_000_000}},
                        {"id": "seg2", "target_timerange": {"start": 4_000_000, "duration": 2_000_000}, "source_timerange": {"start": 0, "duration": 2_000_000}}
                    ]
                }
            ]
        }
        with open(os.path.join(draft_dir, "draft_content.json"), "w", encoding="utf-8") as f:
            json.dump(initial_content, f)

        # 1. Primeira compactação: gap de 0.2s
        res1 = core.compact_draft_audios(draft_dir, gap_seconds=0.2)
        self.assertTrue(res1["ok"])
        self.assertTrue(res1.get("is_first_backup"))
        self.assertTrue(os.path.isfile(os.path.join(draft_dir, "draft_content.json.original")))
        self.assertTrue(os.path.isfile(os.path.join(draft_dir, "draft_content.json.bak")))

        # 2. Segunda compactação: gap de 0.5s
        res2 = core.compact_draft_audios(draft_dir, gap_seconds=0.5)
        self.assertTrue(res2["ok"])
        self.assertFalse(res2.get("is_first_backup"))

        # Verifica que o backup original permaneceu com o start virgem (1_000_000), e NÃO a versão da 1ª compactação!
        with open(os.path.join(draft_dir, "draft_content.json.original"), "r", encoding="utf-8") as f:
            orig = json.load(f)
        self.assertEqual(orig["tracks"][0]["segments"][0]["target_timerange"]["start"], 1_000_000)

        # 3. Restaura o backup
        rest_res = core.restore_draft_backup(draft_dir)
        self.assertTrue(rest_res["ok"])

        # O arquivo atual draft_content.json volta a ter o start original virgem (1_000_000)
        with open(os.path.join(draft_dir, "draft_content.json"), "r", encoding="utf-8") as f:
            final_content = json.load(f)
        self.assertEqual(final_content["tracks"][0]["segments"][0]["target_timerange"]["start"], 1_000_000)

    def test_handle_drop_path(self):
        api = converter_gui.Api()
        # Teste com JSON
        json_file = os.path.join(self.test_dir, "meu_projeto_teste.json")
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump({"fps": 30, "formato": "9:16"}, f)

        res_json = api.handle_drop_path(json_file)
        self.assertEqual(res_json["type"], "json")
        self.assertEqual(res_json["data"]["auto_proj_name"], "meu_projeto_teste")
        self.assertEqual(res_json["data"]["resolution"], "1080x1920")
        self.assertEqual(res_json["data"]["fps"], "30")

        # Teste com Pasta Comum
        res_folder = api.handle_drop_path(self.test_dir)
        self.assertEqual(res_folder["type"], "folder")

        # Teste com Pasta de Draft CapCut
        draft_dir = os.path.join(self.test_dir, "draft_drop_test")
        os.makedirs(draft_dir, exist_ok=True)
        with open(os.path.join(draft_dir, "draft_content.json"), "w", encoding="utf-8") as f:
            json.dump({}, f)
        res_draft = api.handle_drop_path(draft_dir)
        self.assertEqual(res_draft["type"], "draft")

        # Teste com Arquivo de Vídeo
        video_file = os.path.join(self.test_dir, "video_teste.mp4")
        with open(video_file, "wb") as f:
            f.write(b"\x00" * 100)
        res_vid = api.handle_drop_path(video_file)
        self.assertEqual(res_vid["type"], "video")

    def test_ffmpeg_audio_extraction_and_stitching(self):
        # 1. Verifica localização do FFmpeg
        ffmpeg = core.get_ffmpeg_bin()
        self.assertTrue(os.path.isfile(ffmpeg))

        # 2. Cria 2 arquivos de áudio WAV sintéticos dentro de textReading (simulando estrutura nativa do CapCut)
        draft_dir = os.path.join(self.test_dir, "draft_stitch_test")
        text_reading_dir = os.path.join(draft_dir, "textReading")
        os.makedirs(text_reading_dir, exist_ok=True)
        wav1 = os.path.join(text_reading_dir, "fala1.wav")
        wav2 = os.path.join(text_reading_dir, "fala2.wav")
        sample_rate = 44100
        for p in (wav1, wav2):
            with wave.open(p, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(b"\x00\x00" * int(sample_rate * 1.0)) # 1s cada

        # 3. Cria um mock de projeto do CapCut com placeholder nativo (##_draftpath_placeholder_...##)
        draft_content = {
            "duration": 5_000_000,
            "materials": {
                "audios": [
                    {"id": "mat1", "path": "##_draftpath_placeholder_TEST-UUID_##/textReading/fala1.wav"},
                    {"id": "mat2", "path": "##_draftpath_placeholder_TEST-UUID_##/textReading/fala2.wav"}
                ]
            },
            "tracks": [
                {
                    "type": "audio",
                    "segments": [
                        {"id": "s1", "material_id": "mat1", "target_timerange": {"start": 0, "duration": 1_000_000}, "source_timerange": {"start": 0, "duration": 1_000_000}},
                        {"id": "s2", "material_id": "mat2", "target_timerange": {"start": 1_500_000, "duration": 1_000_000}, "source_timerange": {"start": 0, "duration": 1_000_000}}
                    ]
                }
            ]
        }
        with open(os.path.join(draft_dir, "draft_content.json"), "w", encoding="utf-8") as f:
            json.dump(draft_content, f)

        # 4. Testa inspect_draft_for_audio_extraction
        insp = core.inspect_draft_for_audio_extraction(draft_dir)
        self.assertTrue(insp["ok"])
        self.assertEqual(insp["tts_count"], 2)

        # 5. Testa stitch_tts_from_draft para MP3 (320k)
        out_mp3 = os.path.join(self.test_dir, "master_test.mp3")
        res_stitch = core.stitch_tts_from_draft(
            draft_folder=draft_dir,
            output_path=out_mp3,
            audio_format="mp3",
            bitrate="320k",
            gap_seconds=0.1
        )
        self.assertTrue(res_stitch["ok"])
        self.assertTrue(os.path.isfile(out_mp3))
        self.assertGreater(os.path.getsize(out_mp3), 1000)

        # 6. Testa stitch_tts_from_draft para WAV
        out_wav = os.path.join(self.test_dir, "master_test.wav")
        res_wav = core.stitch_tts_from_draft(
            draft_folder=draft_dir,
            output_path=out_wav,
            audio_format="wav"
        )
        self.assertTrue(res_wav["ok"])
        self.assertTrue(os.path.isfile(out_wav))
        self.assertGreater(os.path.getsize(out_wav), 1000)

        # 7. Testa extract_audio_from_video
        test_video = os.path.join(self.test_dir, "test_render.mp4")
        subprocess.run([
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "0.5",
            "-c:v", "libx264", "-c:a", "aac",
            test_video
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        self.assertTrue(os.path.isfile(test_video))
        out_from_vid = os.path.join(self.test_dir, "extracted_from_vid.mp3")
        res_ext = core.extract_audio_from_video(test_video, out_from_vid, "mp3", "320k")
        self.assertTrue(res_ext["ok"])
        self.assertTrue(os.path.isfile(out_from_vid))
        self.assertGreater(os.path.getsize(out_from_vid), 1000)

    def test_stitch_audio_folder_alphabetical(self):
        """Testa a inspeção e junção de áudios avulsos de uma pasta em ordem alfabética natural."""
        audio_dir = os.path.join(self.test_dir, "tts_folder_test")
        os.makedirs(audio_dir, exist_ok=True)

        sample_rate = 44100
        # Cria arquivos com numeração que desafia ordenação lexicográfica padrão (1, 2, 10)
        file_names = ["capitulo_10.wav", "capitulo_1.wav", "capitulo_2.wav"]
        for fn in file_names:
            fp = os.path.join(audio_dir, fn)
            with wave.open(fp, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(b"\x00\x00" * int(sample_rate * 0.5))

        # 1. Inspeciona a pasta
        insp = core.inspect_audio_folder(audio_dir)
        self.assertTrue(insp["ok"])
        self.assertEqual(insp["total_count"], 3)

        # 2. Verifica se a ordenação natural foi aplicada: 1, 2, 10 (e não 1, 10, 2)
        ordered_names = [f["name"] for f in insp["files"]]
        self.assertEqual(ordered_names, ["capitulo_1.wav", "capitulo_2.wav", "capitulo_10.wav"])

        # 3. Testa junção para MP3
        out_unified_mp3 = os.path.join(self.test_dir, "narracao_unificada.mp3")
        res_stitch = core.stitch_audio_folder(
            folder_path=audio_dir,
            output_path=out_unified_mp3,
            audio_format="mp3",
            bitrate="320k",
            gap_seconds=0.1
        )
        self.assertTrue(res_stitch["ok"])
        self.assertEqual(res_stitch["files_count"], 3)
        self.assertTrue(os.path.isfile(out_unified_mp3))
        self.assertGreater(os.path.getsize(out_unified_mp3), 1000)

        # 4. Testa junção para WAV
        out_unified_wav = os.path.join(self.test_dir, "narracao_unificada.wav")
        res_wav = core.stitch_audio_folder(
            folder_path=audio_dir,
            output_path=out_unified_wav,
            audio_format="wav"
        )
        self.assertTrue(res_wav["ok"])
        self.assertTrue(os.path.isfile(out_unified_wav))
        self.assertGreater(os.path.getsize(out_unified_wav), 1000)

    def test_stitch_capcut_online_video_mp4_parts(self):
        """Testa junção e extração de áudio de múltiplos arquivos de vídeo .MP4 (CapCut Online TTS)."""
        ffmpeg = core.get_ffmpeg_bin()
        self.assertTrue(os.path.isfile(ffmpeg))

        vid_dir = os.path.join(self.test_dir, "capcut_online_parts")
        os.makedirs(vid_dir, exist_ok=True)

        # Gera 3 partes de vídeo MP4 sintéticas com áudio em ordem desafiadora (1, 2, 10)
        vnames = ["fala_online_10.mp4", "fala_online_1.mp4", "fala_online_2.mp4"]
        for vn in vnames:
            vp = os.path.join(vid_dir, vn)
            subprocess.run([
                ffmpeg, "-y",
                "-f", "lavfi", "-i", "testsrc=size=160x120:rate=15",
                "-f", "lavfi", "-i", "sine=frequency=800:duration=0.5",
                "-c:v", "libx264", "-c:a", "aac",
                "-t", "0.5", vp
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertTrue(os.path.isfile(vp))

        # 1. Inspeciona a pasta de vídeos
        insp = core.inspect_audio_folder(vid_dir)
        self.assertTrue(insp["ok"])
        self.assertEqual(insp["total_count"], 3)
        self.assertEqual(insp["video_count"], 3)
        self.assertEqual(insp["audio_count"], 0)

        # 2. Verifica a ordenação natural dos vídeos MP4: 1, 2, 10
        ordered = [f["name"] for f in insp["files"]]
        self.assertEqual(ordered, ["fala_online_1.mp4", "fala_online_2.mp4", "fala_online_10.mp4"])
        self.assertTrue(all(f["is_video"] for f in insp["files"]))

        # 3. Executa a junção e extração direta para MP3 (320k)
        out_tts_master = os.path.join(self.test_dir, "master_capcut_online.mp3")
        res = core.stitch_audio_folder(
            folder_path=vid_dir,
            output_path=out_tts_master,
            audio_format="mp3",
            bitrate="320k",
            gap_seconds=0.1
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["files_count"], 3)
        self.assertTrue(os.path.isfile(out_tts_master))
        self.assertGreater(os.path.getsize(out_tts_master), 1000)


class TestCleanEnvironmentGenericUser(unittest.TestCase):
    """Testa o comportamento em um ambiente limpo (computador de outro usuário, sem CapCut pré-instalado)."""

    def test_clean_environment_generic_user(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            fake_env = {
                "USERPROFILE": str(tmp_path),
                "LOCALAPPDATA": str(tmp_path / "AppData" / "Local"),
                "HOME": str(tmp_path),
            }
            temp_config = tmp_path / "converter_config.json"

            with mock.patch.dict(os.environ, fake_env, clear=False), \
                 mock.patch("pathlib.Path.home", return_value=tmp_path), \
                 mock.patch("converter.CONFIG_PATH", temp_config):

                # 1. get_ffmpeg_bin()
                # a) quando não há ffmpeg.exe na pasta do app
                fake_app_empty = tmp_path / "app_empty"
                fake_app_empty.mkdir(parents=True, exist_ok=True)
                fake_py_empty = fake_app_empty / "converter.py"
                fake_py_empty.write_text("# fake", encoding="utf-8")

                with mock.patch("converter.__file__", str(fake_py_empty)):
                    ffmpeg_res = core.get_ffmpeg_bin()
                    self.assertEqual(ffmpeg_res, "")

                # b) quando há ffmpeg.exe colocado na pasta do app
                fake_app_with = tmp_path / "app_with"
                fake_app_with.mkdir(parents=True, exist_ok=True)
                fake_ffmpeg_exe = fake_app_with / "ffmpeg.exe"
                fake_ffmpeg_exe.write_text("fake binary", encoding="utf-8")
                fake_py_with = fake_app_with / "converter.py"
                fake_py_with.write_text("# fake", encoding="utf-8")

                with mock.patch("converter.__file__", str(fake_py_with)):
                    ffmpeg_found = core.get_ffmpeg_bin()
                    self.assertEqual(Path(ffmpeg_found).resolve(), fake_ffmpeg_exe.resolve())

                # 2. launch_capcut() sem CapCut instalado retorna need_path=True e não lança exceção
                res_launch = core.launch_capcut()
                self.assertFalse(res_launch.get("ok"))
                self.assertTrue(res_launch.get("need_path"))
                self.assertIn("error", res_launch)

                # 3. set_capcut_path() com caminho inexistente retorna ok=False
                res_invalid = core.set_capcut_path(str(tmp_path / "capcut_fantasma.exe"))
                self.assertFalse(res_invalid.get("ok"))
                self.assertIn("error", res_invalid)

                # 4. set_capcut_path() com caminho válido grava no config e é usado na próxima chamada
                fake_capcut_exe = tmp_path / "MeusProgramas" / "CapCut.exe"
                fake_capcut_exe.parent.mkdir(parents=True, exist_ok=True)
                fake_capcut_exe.write_text("fake capcut executable", encoding="utf-8")

                res_valid = core.set_capcut_path(str(fake_capcut_exe))
                self.assertTrue(res_valid.get("ok"))
                self.assertEqual(core.load_config().get("capcut_path"), str(fake_capcut_exe.resolve()))

                # Agora launch_capcut() usa o caminho salvo no config
                with mock.patch("os.startfile", create=True) as mock_startfile:
                    res_launch_saved = core.launch_capcut()
                    self.assertTrue(res_launch_saved.get("ok"))
                    self.assertEqual(res_launch_saved.get("path"), str(fake_capcut_exe.resolve()))
                    mock_startfile.assert_called_once_with(str(fake_capcut_exe.resolve()))

                # 5. Teste de apontar pasta draft manualmente e persistência
                custom_drafts = tmp_path / "OutroLocal" / "com.lveditor.draft"
                custom_drafts.mkdir(parents=True, exist_ok=True)
                res_draft = core.set_drafts_dir(str(custom_drafts))
                self.assertTrue(res_draft.get("ok"))
                self.assertEqual(core.load_config().get("drafts_dir"), str(custom_drafts.resolve()))
                self.assertEqual(core.find_capcut_drafts(), str(custom_drafts.resolve()))

                # 6. Teste da interface (Api.launch_capcut com need_path)
                bridge = converter_gui.Api()
                # Simula cancelamento do seletor
                with mock.patch("converter.load_config", return_value={}), \
                     mock.patch("converter_gui.select_file", return_value=""):
                    bridge_cancel = bridge.launch_capcut()
                    self.assertFalse(bridge_cancel.get("ok"))
                    self.assertTrue(bridge_cancel.get("need_path"))

                # Simula usuário escolhendo o executável pelo seletor
                with mock.patch("converter.load_config", return_value={}), \
                     mock.patch("converter_gui.select_file", return_value=str(fake_capcut_exe)), \
                     mock.patch("os.startfile", create=True) as mock_startfile_gui:
                    bridge_success = bridge.launch_capcut()
                    self.assertTrue(bridge_success.get("ok"))
                    self.assertEqual(bridge_success.get("path"), str(fake_capcut_exe.resolve()))


if __name__ == "__main__":
    unittest.main()
