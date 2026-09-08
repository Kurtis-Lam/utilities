import os

# Prevent multi-threading memory expansion across CPU cores     
os.environ["OMP_NUM_THREADS"] = "1" 
os.environ["MKL_NUM_THREADS"] = "1" 
os.environ["OPENBLAS_NUM_THREADS"] = "1" 
os.environ["VECLIB_MAXIMUM_THREADS"] = "1" 
os.environ["NUMEXPR_NUM_THREADS"] = "1" 

import asyncio 
import gc 
import io 
import json 
import re 
from pathlib import Path 
import aiohttp 

def resolve_file_path(filename: str) -> Path: 
    """Searches for a file in the cog folder first, then falls back to current working directory."""     
    cog_dir_path = Path(__file__).resolve().parent / filename 
    root_dir_path = Path.cwd() / filename 

    if cog_dir_path.exists(): 
        return cog_dir_path 
    if root_dir_path.exists(): 
        return root_dir_path 

    raise FileNotFoundError( 
        f"Could not locate '{filename}'. Checked:\n" 
        f"  - {cog_dir_path}\n" 
        f"  - {root_dir_path}" 
    )     


def format_name(name: str) -> str: 
    """Capitalizes names correctly while preserving apostrophes, hyphens, colons, and percentages."""     
    if not name: 
        return "" 

    words = name.split(" ") 
    formatted_words = [] 

    for word in words: 
        hyphen_parts = word.split("-") 
        formatted_hyphen_parts = [] 

        for part in hyphen_parts: 
            colon_parts = part.split(":") 
            formatted_colon_parts = [] 

            for c_part in colon_parts: 
                if "'" in c_part: 
                    apo_parts = c_part.split("'") 
                    formatted_colon_parts.append( 
                        apo_parts[0].capitalize() + "'" + apo_parts[1].lower() 
                    )     
                else:
                    formatted_colon_parts.append(c_part.capitalize()) 

            formatted_hyphen_parts.append(":".join(formatted_colon_parts)) 

        formatted_words.append("-".join(formatted_hyphen_parts)) 

    formatted_str = " ".join(formatted_words) 
    formatted_str = re.sub(r"\b(Jangmo|Hakamo|Kommo)-O\b", r"\1-o", formatted_str) 
    return formatted_str 


def extract_pokemon_from_text(text: str) -> str | None: 
    """Extracts the Pokémon name from caught/fled messages."""     
    cleaned_text = re.sub(r"<a?:[a-zA-Z0-9_]+:\d+>", "", text) 
    cleaned_text = re.sub(r"[♂♀♂️♀️✨]", "", cleaned_text) 

    caught_match = re.search( 
        r"you caught a level \d+\s+([^\(!]+?)\s*(?:\(|\!)", 
        cleaned_text, 
        re.IGNORECASE, 
    )     
    if caught_match: 
        return caught_match.group(1).strip().lower() 

    fled_match = re.search( 
        r"wild\s+([^\!\.\n]+?)\s+fled", cleaned_text, re.IGNORECASE 
    )     
    if fled_match: 
        return fled_match.group(1).strip().lower() 

    return None 


class PokemonRecognizer: 
    def __init__(self): 
        self.ort_session = None 
        self.input_name = None 
        self.db_vectors = None 
        self.db_names = None 
        self.converts_sorted = [] 
        self.pokevars = set() 
        self.pokeinfo = {} 

    def _preprocess_image(self, img, target_size: int = 224):
        """Preprocesses image for CLIP Vision Model using inline dynamic imports to reduce base memory."""     
        import numpy as np     
        from PIL import Image     

        img = img.convert("RGB") 

        width, height = img.size 
        zoom_factor = 0.85 
        new_size = int(min(width, height) * zoom_factor) 
        left = (width - new_size) // 2 
        top = (height - new_size) // 2 
        
        img = img.crop((left, top, left + new_size, top + new_size)) 
        img = img.resize((target_size, target_size), Image.Resampling.BICUBIC) 

        # Allocate single float32 array
        arr = np.asarray(img, dtype=np.float32)

        # In-place normalization     
        arr /= 255.0
        arr -= np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
        arr /= np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

        arr = arr.transpose(2, 0, 1)  # HWC -> CHW     
        return np.expand_dims(arr, axis=0) 

    def load_resources(self): 
        """Loads lightweight ONNX model & memory-mapped vector db from disk."""     
        import numpy as np     
        import onnxruntime as ort     

        # 1. Load ONNX Vision Model with low-memory settings
        try: 
            model_path = resolve_file_path("models/clip_vision_quantized.onnx")     
            opts = ort.SessionOptions() 
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL     
            opts.graph_optimization_level = ( 
                ort.GraphOptimizationLevel.ORT_ENABLE_BASIC 
            )     
            opts.intra_op_num_threads = 1     
            opts.inter_op_num_threads = 1     
            opts.enable_cpu_mem_arena = False     
            opts.enable_mem_pattern = False

            opts.add_session_config_entry("session.enable_cpu_mem_arena", "0")     
            opts.add_session_config_entry( 
                "session.use_device_allocator_for_initializers", "0" 
            )     

            self.ort_session = ort.InferenceSession( 
                str(model_path), 
                sess_options=opts, 
                providers=["CPUExecutionProvider"], 
            )     
            self.input_name = self.ort_session.get_inputs()[0].name     
        except Exception as e: 
            print(f"[Recognize] Warning: Could not load models/clip_vision_quantized.onnx ({e})")     

        # 2. Memory-map vectors directly from disk without consuming RAM
        try: 
            vectors_path = resolve_file_path("models/pokemon_db_vectors.npy")
            names_path = resolve_file_path("models/pokemon_db_names.json")

            self.db_vectors = np.load(str(vectors_path), mmap_mode="r")
            
            with open(names_path, "r", encoding="utf-8") as f:
                self.db_names = json.load(f)
        except Exception as e: 
            print(f"[Recognize] Warning: Could not memory-map vector db ({e})")

        # 3. Load constant local datasets
        try:
            converts_path = resolve_file_path("converts.json")
            with open(converts_path, "r", encoding="utf-8") as f:
                converts = json.load(f)
                self.converts_sorted = sorted(
                    converts.items(), key=lambda x: len(x[0]), reverse=True
                )
        except Exception:
            self.converts_sorted = []

        try:
            pokevars_path = resolve_file_path("pokevars.json")
            with open(pokevars_path, "r", encoding="utf-8") as f:
                pokevars_list = json.load(f)
                self.pokevars = {name.strip().lower() for name in pokevars_list}
        except Exception:
            self.pokevars = set()

        try:
            pokeinfo_path = resolve_file_path("pokeinfo.json")
            with open(pokeinfo_path, "r", encoding="utf-8") as f:
                pokeinfo_raw = json.load(f)
                self.pokeinfo = {
                    k.strip().lower(): {
                        "types": v.get("types", []) if isinstance(v, dict) else [],
                        "region": v.get("region", "") if isinstance(v, dict) else ""
                    }
                    for k, v in pokeinfo_raw.items()
                }
        except Exception:
            self.pokeinfo = {}

        gc.collect()     

    def apply_conversions(self, name: str) -> str: 
        if not self.converts_sorted: 
            return name     

        current_name = name 
        for key, val in self.converts_sorted:     
            if key.lower() in current_name.lower(): 
                pattern = re.compile(re.escape(key), re.IGNORECASE)     
                current_name = pattern.sub(val, current_name)     

        return current_name     

    def predict_from_image_bytes(self, image_bytes: bytes) -> tuple[str, float]: 
        import numpy as np     
        from PIL import Image     

        if self.ort_session is None or self.db_vectors is None:     
            raise RuntimeError("ONNX Session or Database not loaded properly.")     

        with Image.open(io.BytesIO(image_bytes)) as img:     
            input_tensor = self._preprocess_image(img, target_size=224)     

        outputs = self.ort_session.run(None, {self.input_name: input_tensor})     
        target_vector = outputs[0]     

        norm = np.linalg.norm(target_vector, axis=-1, keepdims=True)     
        if norm != 0: 
            target_vector = target_vector / norm     

        target_vector = target_vector.astype(np.float16)     
        similarities = np.dot(target_vector, self.db_vectors.T).squeeze()     
        best_match_idx = int(np.argmax(similarities))     
        confidence = float(similarities[best_match_idx])     

        raw_name = str(self.db_names[best_match_idx]) 
        converted_name = self.apply_conversions(raw_name)     

        del input_tensor, outputs, target_vector, similarities
        gc.collect()

        return converted_name, confidence     

    async def identify_from_url( 
        self, session: aiohttp.ClientSession, url: str 
    ) -> tuple[str, float]:
        async with session.get(url) as resp:     
            if resp.status != 200:     
                raise ValueError(f"Failed to download image. HTTP {resp.status}")     
            image_bytes = await resp.read()     

        return await asyncio.to_thread( 
            self.predict_from_image_bytes, image_bytes 
        )