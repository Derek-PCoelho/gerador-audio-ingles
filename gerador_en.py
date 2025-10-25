# -*- coding: utf-8 -*-
# --- ENGLISH AUDIO GENERATOR (v7.1 - Network Resiliency) ---

import os
import pathlib 
import re
import base64
import requests
import time
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, Frame, Label
import concurrent.futures
import subprocess

try:
    import docx
    from num2words import num2words
    from moviepy.editor import AudioFileClip, concatenate_audioclips
    import pygame
    import ttkbootstrap as ttk
    from ttkbootstrap.constants import *
except ImportError as e:
    tk.Tk().withdraw()
    messagebox.showerror(
        "Dependency Error",
        f"Library not found: {e}\n\nPlease run 'pip install -r requirements.txt' to install dependencies.",
    )
    exit()

# ==============================================================================
# GLOBAL CONFIGURATIONS
# ==============================================================================
GOOGLE_API_KEY = "AIzaSyDQIQa8D4JcU3UKUHnhapYJHh60Lz2Hc3I"
LANGUAGE_PROFILES = {
    "en": {
        "force_voice_name": "en-US-Chirp3-HD-Charon",
        "num2words_lang": "en",
        "chapter_keywords": "Chapter|Section|Part|Conclusion",
    }
}
lang_key = "en"
profile = LANGUAGE_PROFILES[lang_key]
VOICE_NAME = profile["force_voice_name"]
LANGUAGE_CODE = "-".join(VOICE_NAME.split("-")[0:2])
NUM2WORDS_LANG = profile["num2words_lang"]
CHAPTER_MARKERS_REGEX = rf'^\s*({profile["chapter_keywords"]})\s*[\d:]*\s*[–—\-:]*\s*.*'
CTA_INTRO_MARKERS = [
    "comment below",
    "Comment this right now",
    "Say it, write it, declare it",
]
CTA_MEIO_MARKER = "[CTA MEIO AQUI]"
CTA_FINAL_MARKER = "[CTA FIM AQUI]"
TMP_DIR = pathlib.Path("./tts_temp_en")
TARGET_SR = 24000

### MUDANÇA: Reduzindo o número de workers para evitar sobrecarga na rede ###
MAX_WORKERS = 4


# ==============================================================================
# SUPPORT FUNCTIONS
# ==============================================================================
# (As funções de parse, normalize, etc., permanecem as mesmas)
def normalize_and_clean_text(text: str) -> str:
    text = text.replace("’", "'")
    text = text.replace("\r", "\n").replace("<", "").replace(">", "")
    text = re.sub(r"[\s\t]*\n[\s\t]*", " ", text)
    return text.strip()


def convert_numbers_to_words(text: str, lang: str) -> str:
    if not lang:
        return text
    return re.sub(r"\b\d+\b", lambda m: num2words(int(m.group(0)), lang=lang), text)


def parse_script(
    full_text: str,
    cta_meio: str,
    cta_final: str,
    chapter_regex: str,
    cta_intro_markers: list,
) -> tuple[str, list]:
    content, _, _ = full_text.partition(cta_final)
    content = content.strip()
    if not content:
        return "Untitled Script", []
    script_title_parts = content.split("\n", 1)
    script_title = (
        script_title_parts[0].strip() if script_title_parts else "Untitled Script"
    )
    markers = list(
        re.finditer(chapter_regex, content, flags=re.IGNORECASE | re.MULTILINE)
    )
    final_segments = []
    first_marker_pos = markers[0].start() if markers else len(content)
    intro_full_text = content[:first_marker_pos].strip()
    if intro_full_text:
        intro_parts = intro_full_text.split("\n", 1)
        intro_body_text = (
            intro_parts[1].strip() if len(intro_parts) > 1 else intro_full_text
        )
        if intro_body_text.strip():
            intro_segment = {"title": "Introduction", "parts": []}
            cta_found_marker = next(
                (
                    m
                    for m in cta_intro_markers
                    if re.search(re.escape(m), intro_body_text, re.IGNORECASE)
                ),
                None,
            )
            if cta_found_marker:
                body, _, cta_and_rest = intro_body_text.partition(cta_found_marker)
                if body.strip():
                    intro_segment["parts"].append(
                        {"type": "body", "text": body.strip()}
                    )
                if cta_and_rest.strip():
                    intro_segment["parts"].append(
                        {
                            "type": "cta",
                            "text": f"{cta_found_marker}{cta_and_rest}".strip(),
                        }
                    )
            else:
                intro_segment["parts"].append({"type": "body", "text": intro_body_text})
            if intro_segment["parts"]:
                final_segments.append(intro_segment)
    for i, marker in enumerate(markers):
        chapter_title = marker.group(0).strip()
        start_pos = marker.end()
        end_pos = markers[i + 1].start() if i + 1 < len(markers) else len(content)
        chapter_body_text = content[start_pos:end_pos].strip()
        chapter_body_text, _, _ = chapter_body_text.partition(cta_meio)
        chapter_body_text = chapter_body_text.strip()
        chapter_segment = {"title": chapter_title, "parts": []}
        if chapter_title.strip():
            chapter_segment["parts"].append({"type": "title", "text": chapter_title})
        if chapter_body_text.strip():
            chapter_segment["parts"].append({"type": "body", "text": chapter_body_text})
        if chapter_segment["parts"]:
            final_segments.append(chapter_segment)
    return script_title, final_segments


def run_ffmpeg(command: list):
    subprocess.run(
        command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def split_into_chunks(text: str, max_chars: int = 4800):
    if len(text) < max_chars:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks, current_chunk = [], ""
    for sentence in sentences:
        if len(current_chunk) + len(sentence) + 1 < max_chars:
            current_chunk += sentence + " "
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = sentence + " "
    if current_chunk:
        chunks.append(current_chunk.strip())
    return chunks


### MUDANÇA: Função agora tem lógica de retentativa automática ###
def generate_audio_for_chunk(text: str, output_path: pathlib.Path, max_retries=3):
    url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_API_KEY}"
    headers = {"Content-Type": "application/json; charset=utf-8"}
    payload = {
        "input": {"text": text},
        "voice": {"languageCode": LANGUAGE_CODE, "name": VOICE_NAME},
        "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": TARGET_SR},
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=180)
            response.raise_for_status()
            audio_content = base64.b64decode(response.json()["audioContent"])
            with open(output_path, "wb") as f:
                f.write(audio_content)
            return  # Sucesso, sai da função
        except requests.exceptions.RequestException as e:
            # Se for a última tentativa, o erro será lançado e capturado pelo relatório final
            if attempt == max_retries - 1:
                raise e
            # Espera um pouco antes de tentar novamente
            time.sleep(2 * (attempt + 1))  # Espera 2, 4 segundos...


def safe_rmtree(path, max_retries=5, delay=0.2):
    for _ in range(max_retries):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            time.sleep(delay)
    shutil.rmtree(path)


# ==============================================================================
# GUI APPLICATION CLASS
# ==============================================================================
# (Nenhuma outra alteração no resto do código. Apenas as duas mudanças acima)
class AudioGeneratorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("English Audio Generator (v7.1 Network Resilient)")
        self.root.geometry("900x700")
        pygame.mixer.init()

        self.script_title = "untitled_script"
        self.generated_segments_data = []
        self.error_log = []

        main_frame = ttk.Frame(root, padding="15 15 15 15")
        main_frame.pack(fill=tk.BOTH, expand=True)

        action_frame = ttk.Frame(main_frame)
        action_frame.pack(fill=tk.X, pady=(0, 10))
        list_frame = ttk.Frame(main_frame)
        list_frame.pack(fill=tk.BOTH, expand=True)
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=(10, 0))

        self.btn_select = ttk.Button(
            action_frame,
            text="1. Select Script...",
            command=self.select_script,
            bootstyle=PRIMARY,
        )
        self.btn_select.pack(side=tk.LEFT, padx=(0, 10))
        self.btn_finalize = ttk.Button(
            action_frame,
            text="2. Finalize and Save Audios",
            command=self.finalize_audios,
            state=tk.DISABLED,
            bootstyle=INFO,
        )
        self.btn_finalize.pack(side=tk.LEFT)

        self.canvas = tk.Canvas(list_frame, highlightthickness=0, bg=root.cget("bg"))
        scrollbar = ttk.Scrollbar(
            list_frame, orient="vertical", command=self.canvas.yview, bootstyle=ROUND
        )
        self.scrollable_frame = ttk.Frame(self.canvas)
        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.progress_label = ttk.Label(
            status_frame, text="Waiting for a script...", font="-size 10"
        )
        self.progress_label.pack(fill=tk.X)
        self.progress_bar = ttk.Progressbar(
            status_frame, mode="determinate", length=100, bootstyle=PRIMARY
        )
        self.progress_bar.pack(fill=tk.X, pady=(5, 0))

    def run_in_thread(self, target_func, *args):
        threading.Thread(target=target_func, args=args, daemon=True).start()

    def update_progress(self, current, total, start_time):
        progress_percentage = (current / total) * 100
        self.progress_bar["value"] = progress_percentage
        elapsed_time = time.time() - start_time
        time_per_item = elapsed_time / current if current > 0 else 0
        remaining_items = total - current
        remaining_time = remaining_items * time_per_item
        time_str = ""
        if remaining_time > 0:
            mins, secs = divmod(remaining_time, 60)
            time_str = f"~{int(mins)}m {int(secs)}s remaining"
        self.progress_label.config(
            text=f"Generating audio {current}/{total} ({progress_percentage:.1f}%)... {time_str}"
        )
        self.root.update_idletasks()

    def select_script(self):
        filepath = filedialog.askopenfilename(
            title="Select the ENGLISH Script",
            filetypes=(("Word Documents", "*.docx"), ("Text Files", "*.txt")),
        )
        if not filepath:
            return
        self.generated_segments_data = []
        self.error_log = []
        self.redraw_ui_list()
        self.progress_label.config(
            text=f"Analyzing script: {os.path.basename(filepath)}..."
        )
        self.progress_bar["value"] = 0
        self.progress_bar.configure(bootstyle=PRIMARY)
        self.btn_select.config(state=tk.DISABLED)
        self.btn_finalize.config(state=tk.DISABLED)
        self.run_in_thread(self.process_and_generate_audios, filepath)

    def process_and_generate_audios(self, filepath):
        start_time = time.time()
        try:
            TMP_DIR.mkdir(exist_ok=True, parents=True)
            if filepath.lower().endswith(".docx"):
                doc = docx.Document(filepath)
                full_text = "\n".join([p.text for p in doc.paragraphs])
            else:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    full_text = f.read()
            self.script_title, script_parts = parse_script(
                full_text,
                CTA_MEIO_MARKER,
                CTA_FINAL_MARKER,
                CHAPTER_MARKERS_REGEX,
                CTA_INTRO_MARKERS,
            )
            tasks = [
                {"segment": segment, "part": part, "i": i, "j": j}
                for i, segment in enumerate(script_parts)
                for j, part in enumerate(segment.get("parts", []))
            ]
            if not tasks:
                self.root.after(
                    0,
                    lambda: self.progress_label.config(
                        text="❌ Error: No valid text segments were found."
                    ),
                )
                messagebox.showerror(
                    "Parsing Error",
                    "The script was read, but no valid segments were identified.",
                )
                self.root.after(0, lambda: self.btn_select.config(state=tk.NORMAL))
                return
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=MAX_WORKERS
            ) as executor:
                future_to_task = {
                    executor.submit(self.worker_generate_audio, task): task
                    for task in tasks
                }
                completed_count = 0
                total_tasks = len(tasks)
                for future in concurrent.futures.as_completed(future_to_task):
                    completed_count += 1
                    self.root.after(
                        0,
                        self.update_progress,
                        completed_count,
                        total_tasks,
                        start_time,
                    )
                    task = future_to_task[future]
                    try:
                        segment_info = future.result()
                        if segment_info:
                            self.generated_segments_data.append(segment_info)
                    except Exception as exc:
                        self.error_log.append(
                            f"- Failure in '{task['segment']['title']}': {exc}"
                        )
            self.generated_segments_data.sort(key=lambda s: s["filename"])
            self.root.after(0, self.redraw_ui_list)
            if self.error_log:
                messagebox.showerror(
                    "Error Report",
                    "The following errors occurred:\n\n" + "\n".join(self.error_log),
                )
                self.root.after(
                    0,
                    lambda: [
                        self.progress_label.config(
                            text=f"❌ Completed with {len(self.error_log)} error(s)."
                        ),
                        self.progress_bar.configure(bootstyle=DANGER),
                    ],
                )
            else:
                self.root.after(
                    0,
                    lambda: [
                        self.progress_label.config(text="✅ Generation complete!"),
                        self.progress_bar.configure(bootstyle=SUCCESS),
                    ],
                )
        except Exception as e:
            messagebox.showerror("Critical Error", f"An error occurred: {e}")
            self.root.after(
                0, lambda: self.progress_label.config(text="❌ Critical error.")
            )
        finally:
            self.root.after(0, lambda: self.btn_select.config(state=tk.NORMAL))
            if self.generated_segments_data:
                self.root.after(0, lambda: self.btn_finalize.config(state=tk.NORMAL))

    def worker_generate_audio(self, task):
        segment, part, i, j = task["segment"], task["part"], task["i"], task["j"]
        normalized_text = normalize_and_clean_text(part["text"])
        final_text = convert_numbers_to_words(normalized_text, NUM2WORDS_LANG)
        if not final_text.strip():
            return None
        text_chunks = split_into_chunks(final_text)
        chunk_paths = []
        temp_chunk_dir = TMP_DIR / f"chunks_{i}_{j}"
        temp_chunk_dir.mkdir(exist_ok=True)
        for idx, chunk in enumerate(text_chunks):
            if not chunk.strip():
                continue
            chunk_path = temp_chunk_dir / f"chunk_{idx}.wav"
            generate_audio_for_chunk(chunk, chunk_path)
            chunk_paths.append(chunk_path)
        if not chunk_paths:
            return None
        safe_title = re.sub(r"[\s\W]+", "_", segment["title"]).lower()
        part_type = part.get("type", "part")
        filename = f"{i:02d}_{j:02d}_{safe_title}_{part_type}.wav"
        output_path = TMP_DIR / filename
        if len(chunk_paths) == 1:
            shutil.move(chunk_paths[0], output_path)
        else:
            concat_list_path = temp_chunk_dir / "concat_list.txt"
            with open(concat_list_path, "w", encoding="utf-8") as f:
                for path in chunk_paths:
                    f.write(f"file '{path.resolve()}'\n")
            ffmpeg_command = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list_path),
                "-c",
                "copy",
                str(output_path),
            ]
            run_ffmpeg(ffmpeg_command)
        safe_rmtree(temp_chunk_dir)
        with AudioFileClip(str(output_path)) as clip:
            duration = clip.duration
        return {
            "title": segment["title"],
            "type": part_type,
            "text": final_text,
            "path": output_path,
            "duration": duration,
            "filename": filename,
            "approved": tk.BooleanVar(value=True),
        }

    def redraw_ui_list(self):
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        for i, seg_info in enumerate(self.generated_segments_data):
            self.add_segment_to_ui(seg_info, i)

    def play_audio(self, path):
        if pygame.mixer.music.get_busy():
            pygame.mixer.music.unpause()
        else:
            try:
                pygame.mixer.music.load(path)
                pygame.mixer.music.play()
            except Exception as e:
                self.progress_label.config(text=f"Error playing audio: {e}")

    def pause_audio(self):
        if pygame.mixer.music.get_busy():
            pygame.mixer.music.pause()

    def download_segment(self, segment_info):
        save_path = filedialog.asksaveasfilename(
            initialfile=segment_info["filename"],
            defaultextension=".wav",
            filetypes=[("WAV files", "*.wav")],
        )
        if save_path:
            shutil.copy(segment_info["path"], save_path)
            messagebox.showinfo(
                "Download Complete",
                f"Audio '{segment_info['filename']}' saved successfully!",
            )

    def add_segment_to_ui(self, segment_info, index):
        frame = ttk.Frame(self.scrollable_frame, padding=10)
        frame.pack(fill=tk.X, padx=5, pady=4)
        title = f"{index+1}. {segment_info['title']} ({segment_info['type']}) - {segment_info['duration']:.2f}s"
        ttk.Label(frame, text=title, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(anchor=tk.W, pady=(8, 0))
        ttk.Button(
            btn_frame,
            text="▶ Play / Resume",
            command=lambda p=segment_info["path"]: self.run_in_thread(
                self.play_audio, str(p)
            ),
            bootstyle=PRIMARY,
        ).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(
            btn_frame, text="❚❚ Pause", command=self.pause_audio, bootstyle=SECONDARY
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            btn_frame,
            text="Regenerate",
            command=lambda s=segment_info, i=index, f=frame: self.regenerate_single_audio(
                s, i, f
            ),
            bootstyle=WARNING,
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            btn_frame,
            text="Download",
            command=lambda s=segment_info: self.download_segment(s),
            bootstyle=LIGHT,
        ).pack(side=tk.LEFT, padx=5)
        ttk.Checkbutton(
            btn_frame,
            text="Approved",
            variable=segment_info["approved"],
            bootstyle="round-toggle",
        ).pack(side=tk.LEFT, padx=15)

    def regenerate_single_audio(self, segment_info, index, ui_frame):
        pygame.mixer.quit()
        pygame.mixer.init()
        self.progress_label.config(text=f"Regenerating: {segment_info['title']}...")
        self.progress_bar.configure(bootstyle=WARNING)
        self.run_in_thread(self._regenerate_task, segment_info, index, ui_frame)

    def _regenerate_task(self, segment_info, index, ui_frame):
        try:
            data_index = next(
                i
                for i, item in enumerate(self.generated_segments_data)
                if item["path"] == segment_info["path"]
            )
            task_i = int(segment_info["filename"].split("_")[0])
            task_j = int(segment_info["filename"].split("_")[1])
            original_task = {
                "segment": {"title": segment_info["title"]},
                "part": {"text": segment_info["text"], "type": segment_info["type"]},
                "i": task_i,
                "j": task_j,
            }
            new_info = self.worker_generate_audio(original_task)
            if new_info:
                self.generated_segments_data[data_index] = new_info
                new_title = f"{index+1}. {new_info['title']} ({new_info['type']}) - {new_info['duration']:.2f}s"
                self.root.after(
                    0, ui_frame.winfo_children()[0].config, {"text": new_title}
                )
                self.root.after(
                    0,
                    lambda: self.progress_label.config(
                        text=f"✅ Audio '{segment_info['title']}' regenerated."
                    ),
                )
                self.root.after(
                    0, lambda: self.progress_bar.configure(bootstyle=SUCCESS)
                )
        except Exception as e:
            self.root.after(
                0, lambda: messagebox.showerror("Regeneration Error", str(e))
            )
            self.root.after(
                0,
                lambda: self.progress_label.config(
                    text="❌ Error during regeneration."
                ),
            )
            self.root.after(0, lambda: self.progress_bar.configure(bootstyle=DANGER))
        finally:
            self.root.after(0, lambda: self.btn_select.config(state=tk.NORMAL))
            self.root.after(0, lambda: self.btn_finalize.config(state=tk.NORMAL))

    def finalize_audios(self):
        approved_audios = sorted(
            [seg for seg in self.generated_segments_data if seg["approved"].get()],
            key=lambda s: s["filename"],
        )
        if not approved_audios:
            messagebox.showwarning(
                "No Audios", "No audios were approved for finalization."
            )
            return
        output_root_path = filedialog.askdirectory(
            title="Select a folder to save the final audios"
        )
        if not output_root_path:
            return
        pygame.mixer.quit()
        pygame.mixer.init()
        self.progress_label.config(text="Finalizing... Saving audios...")
        self.btn_finalize.config(state=tk.DISABLED)
        self.progress_bar.configure(bootstyle=INFO)
        self.run_in_thread(self._finalize_task, approved_audios, output_root_path)

    def _finalize_task(self, approved_audios, output_root_str):
        audio_clips = []
        final_clip = None
        output_root = pathlib.Path(output_root_str)
        try:
            safe_title = re.sub(r"[^\w\-_\. ]", "_", self.script_title)
            self.root.after(
                0,
                self.progress_label.config,
                {"text": "Step 1/2: Creating master audio..."},
            )
            audio_clips = [AudioFileClip(str(info["path"])) for info in approved_audios]
            final_clip = concatenate_audioclips(audio_clips)
            master_output_path = output_root / f"{safe_title}_final_en.wav"
            final_clip.write_audiofile(
                str(master_output_path), fps=TARGET_SR, logger=None
            )
            self.root.after(
                0,
                self.progress_label.config,
                {"text": "Step 2/2: Saving individual audios..."},
            )
            individual_dir = output_root / f"{safe_title}_individual_audios"
            individual_dir.mkdir(exist_ok=True)
            for info in approved_audios:
                shutil.copy(info["path"], individual_dir / info["filename"])
            shutil.rmtree(TMP_DIR)
            self.root.after(
                0,
                lambda: self.progress_label.config(
                    text="🎉 Process Completed Successfully!"
                ),
            )
            self.root.after(0, lambda: self.progress_bar.configure(bootstyle=SUCCESS))
            self.root.after(0, self.disable_segment_list)
            messagebox.showinfo(
                "Success", f"Audios saved successfully in:\n{output_root.resolve()}"
            )
        except Exception as e:
            self.root.after(
                0, lambda: messagebox.showerror("Finalization Error", str(e))
            )
            self.root.after(
                0,
                lambda: self.progress_label.config(
                    text="❌ Error during finalization."
                ),
            )
            self.root.after(0, lambda: self.progress_bar.configure(bootstyle=DANGER))
        finally:
            if final_clip:
                final_clip.close()
            for clip in audio_clips:
                clip.close()
            self.root.after(0, lambda: self.btn_finalize.config(state=tk.NORMAL))

    def disable_segment_list(self):
        for frame in self.scrollable_frame.winfo_children():
            for widget in frame.winfo_children():
                if isinstance(widget, ttk.Frame):
                    for button in widget.winfo_children():
                        button.config(state=tk.DISABLED)


if __name__ == "__main__":
    root = ttk.Window(themename="flatly")
    app = AudioGeneratorApp(root)
    root.mainloop()
