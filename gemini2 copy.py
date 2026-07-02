import logging
import json
import re
import time
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, firestore

# --- 1. KONFIGURASI ---
cred = credentials.Certificate("invtnewgmn.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log_penelitian-gemini.txt")

# TODO: Isi API Key Google Gemini kamu di sini
genai.configure(api_key="")

# --- 1b. INISIALISASI MODEL GEMINI ---
SYSTEM_PROMPT = """Kamu adalah asisten gudang inventaris.
Tugasmu mengenali perintah user dan mengembalikan JSON yang sesuai.

ATURAN WAJIB:
- Lihat/tampilkan SEMUA barang/stok/inventaris → {"fungsi": "cek_semua"}
- Cek/ada/apakah ada 1 barang tertentu di gudang/db → {"fungsi": "cek", "barang": "nama"}
- Tambah stok dengan jumlah jelas → {"fungsi": "tambah", "barang": "nama", "jumlah": angka}
- Tambah stok TANPA jumlah → {"fungsi": "tambah_tanpa_jumlah", "barang": "nama"}
- Kurangi stok dengan jumlah jelas → {"fungsi": "kurang", "barang": "nama", "jumlah": angka}
- Kurangi stok TANPA jumlah → {"fungsi": "kurang_tanpa_jumlah", "barang": "nama"}
- Hapus/hilangkan barang dari database → {"fungsi": "hapus", "barang": "nama"}
- Tidak berkaitan inventaris → jawab teks biasa.

PENTING: Untuk perintah inventaris, kembalikan JSON saja tanpa teks tambahan."""

model = genai.GenerativeModel(
    model_name="gemini-3.1-flash-lite",
    system_instruction=SYSTEM_PROMPT,
    generation_config={"temperature": 0}
)

# --- FUNGSI LOGGING (Pretty version! Multi-line, rapi, mudah dibaca) ---
def log_ke_file(
    model_name,
    user_input,
    output,
    latency,
    status,
    db_change=None,
    raw_ai_response=None,
    parsed_function=None
):
    log_entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": model_name,
        "input": user_input,
        "raw_ai_response": raw_ai_response,
        "parsed_function": parsed_function,
        "output": output,
        "latency_ms": round(latency * 1000, 2),
        "status": status
    }

    if db_change:
        log_entry["db_change"] = db_change

    separator = "=" * 60

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{separator}\n")
        f.write(f" LOG ENTRY — {log_entry['timestamp']}\n")
        f.write(f"{separator}\n")
        f.write(json.dumps(log_entry, indent=2, ensure_ascii=False))
        f.write("\n")
# --- 2. FUNGSI DATABASE ---
def tambah_stok(nama_barang, jumlah):
    ref = db.collection('stok_barang').document(nama_barang.lower())
    doc = ref.get()
    stok_sebelum = doc.to_dict().get('jumlah', 0) if doc.exists else 0
    ref.set({'jumlah': firestore.Increment(jumlah)}, merge=True)
    stok_sesudah = ref.get().to_dict().get('jumlah', 0)
    pesan = f"✅ Berhasil! Stok {nama_barang} ditambah {jumlah}. Total: {stok_sesudah} unit."
    db_change = {"aksi": "tambah", "barang": nama_barang, "jumlah": jumlah,
                 "stok_sebelum": stok_sebelum, "stok_sesudah": stok_sesudah}
    return pesan, db_change

def kurangi_stok(nama_barang, jumlah):
    ref = db.collection('stok_barang').document(nama_barang.lower())
    doc = ref.get()

    if not doc.exists:
        return f"❌ Barang '{nama_barang}' tidak ditemukan di database.", None

    stok_sebelum = doc.to_dict().get('jumlah', 0)

    if stok_sebelum < jumlah:
        return f"❌ Stok {nama_barang} tidak cukup. Stok saat ini hanya {stok_sebelum} unit.", None

    ref.set({'jumlah': firestore.Increment(-jumlah)}, merge=True)
    stok_sesudah = ref.get().to_dict().get('jumlah', 0)
    pesan = f"✅ Berhasil! Stok {nama_barang} dikurangi {jumlah}. Sisa: {stok_sesudah} unit."
    db_change = {"aksi": "kurang", "barang": nama_barang, "jumlah": jumlah,
                 "stok_sebelum": stok_sebelum, "stok_sesudah": stok_sesudah}
    return pesan, db_change

def hapus_stok(nama_barang):
    ref = db.collection('stok_barang').document(nama_barang.lower())
    doc = ref.get()
    if doc.exists:
        stok_sebelum = doc.to_dict().get('jumlah', 0)
        ref.delete()
        db_change = {"aksi": "hapus", "barang": nama_barang, "stok_sebelum": stok_sebelum}
        return f"Berhasil! Barang '{nama_barang}' telah dihapus dari database.", db_change
    return f"\u274c Barang '{nama_barang}' tidak ditemukan di database.", None

def cek_stok(nama_barang):
    doc = db.collection('stok_barang').document(nama_barang.lower()).get()
    if doc.exists:
        return f"📦 Stok {nama_barang} saat ini: {doc.to_dict().get('jumlah', 0)} unit."
    return f"❌ Barang '{nama_barang}' tidak ditemukan di database."

def tampilkan_semua_stok():
    docs = list(db.collection('stok_barang').stream())
    if not docs:
        return "Gudang kosong, belum ada barang."
    daftar = "\n".join([f"• {doc.id}: {doc.to_dict().get('jumlah', 0)} unit" for doc in docs])
    return f"📦 Daftar Stok Barang:\n{daftar}"

# --- 3. PARSE JSON DARI RESPONS AI ---
def parse_json_response(content):
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
    if not match:
        match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
    if match:
        raw = match.group(1) if '```' in content else match.group(0)
        raw = raw.replace("'", '"').replace('None', 'null')
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return None


# --- 4. LOGIKA BOT ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    model_name = "gemini-3.1-flash-lite"
    start_time = time.time()

    try:
        content = ""
        parsed_function = None

        # Panggil Gemini API
        chat = model.start_chat()
        response = chat.send_message(user_input)

        latency = time.time() - start_time
        content = response.text.strip()
        print(f"DEBUG AI response: {content}")

        data = parse_json_response(content)

        parsed_function = None

        if data:
            parsed_function = data.get("fungsi")

        if data:
            fungsi = data.get('fungsi')
            barang = data.get('barang', '')
            jumlah = data.get('jumlah')
            pesan = ""
            db_change = None
            status = "Database_Action"

            if fungsi == 'cek_semua':
                pesan = tampilkan_semua_stok()

            elif fungsi == 'cek':
                if barang:
                    pesan = cek_stok(barang)
                else:
                    pesan = "Nama barang tidak terdeteksi, coba sebutkan nama barangnya."
                    status = "Error_Input"

            elif fungsi == 'tambah':
                if barang and jumlah:
                    pesan, db_change = tambah_stok(barang, int(jumlah))
                else:
                    pesan = "Data tidak lengkap. Contoh: 'Tambah stok sapu 10'"
                    status = "Error_Input"

            elif fungsi == 'tambah_tanpa_jumlah':
                pesan = f"Tambah berapa unit {barang}? Contoh: 'Tambah stok {barang} 5'"
                status = "Clarification"

            elif fungsi == 'kurang':
                if barang and jumlah:
                    pesan, db_change = kurangi_stok(barang, int(jumlah))
                    if db_change is None:
                        status = "Error_Stok"
                else:
                    pesan = "Data tidak lengkap. Contoh: 'Kurangi stok buku 2'"
                    status = "Error_Input"

            elif fungsi == 'kurang_tanpa_jumlah':
                pesan = f"Kurangi berapa unit {barang}? Contoh: 'Kurangi stok {barang} 2'"
                status = "Clarification"

            elif fungsi == 'hapus':
                if barang:
                    pesan, db_change = hapus_stok(barang)
                    if db_change is None:
                        status = "Error_Stok"
                else:
                    pesan = "Nama barang tidak terdeteksi, coba sebutkan nama barang yang ingin dihapus."
                    status = "Error_Input"

            else:
                pesan = "Perintah tidak dikenali. Coba: 'tampilkan semua stok', 'cek stok sapu', dll."
                status = "Unknown_Function"

            log_ke_file(model_name, user_input, pesan, latency, status, db_change, raw_ai_response=content, parsed_function=parsed_function)
            await update.message.reply_text(pesan)

        else:
            log_ke_file(model_name, user_input, content, latency, "Chat_Response", raw_ai_response=content, parsed_function=parsed_function)
            await update.message.reply_text(content)

    except Exception as e:
        latency = time.time() - start_time
        pesan_error = "Terjadi error, coba lagi ya."
        log_ke_file(model_name, user_input, pesan_error, latency, f"Error: {str(e)}", raw_ai_response=content, parsed_function=parsed_function)
        print(f"ERROR: {e}")
        await update.message.reply_text(pesan_error)

if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    # TODO: Isi Token Bot Telegram kamu di sini
    app = ApplicationBuilder().token("Token Tele").build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    print("Bot Inventaris (Gemini) Berjalan & Logger Aktif!")
    app.run_polling()
