import sqlite3
import logging
import re
import io
import csv
from datetime import datetime, timezone
from typing import List, Tuple

import telegram
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CallbackQueryHandler, filters

# ===== CONFIGURAÇÃO =====
TELEGRAM_TOKEN = "8319337394:AAFrrd64nlaGcMPJyQ5j7247QdFNAVAVTIA"  # <-- substitua pelo seu token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB = "finbot.db"
conn = None

# stopwords para não aprender palavras inúteis
STOPWORDS = {"de", "do", "da", "em", "no", "na", "para", "ao", "à", "e", "com", "o", "a", "um", "uma", "por", "dos", "das"}

# categorias padrões (mapeamento de palavras-chave)
CATEGORIES_EXPENSE = {
    "alimentação": ["mercado", "supermercado", "restaurante", "almoço", "lanchonete", "fastfood", "resto"],
    "transporte": ["uber", "taxi", "ônibus", "onibus", "combustivel", "combustível", "gasolina", "metrô", "metro"],
    "moradia": ["aluguel", "condomínio", "condominio", "iptu"],
    "saúde": ["remédio", "medicamento", "farmácia", "farmacia", "consulta"],
    "lazer": ["cinema", "bar", "show", "lazer"],
    "cartão de crédito": ["cartão", "cartao", "credito", "crédito"]
}
CATEGORIES_INCOME = {
    "salário": ["salário", "salario", "salario", "salário", "salario", "salario"],
    "outros": ["bonus", "bônus", "presente", "devolução"]
}

# ===== BANCO DE DADOS =====
def init_db():
    global conn
    conn = sqlite3.connect(DB, check_same_thread=False)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS transactions(
        id INTEGER PRIMARY KEY,
        user_id TEXT,
        type TEXT,
        amount REAL,
        category TEXT,
        note TEXT,
        date TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS categories_learned(
        word TEXT PRIMARY KEY,
        category TEXT
    )""")
    conn.commit()

def add_transaction(user_id: str, amount: float, category: str, note: str = "", ttype: str = None):
    if ttype is None:
        ttype = "income" if amount >= 0 else "expense"
    date = datetime.now(timezone.utc).isoformat()
    c = conn.cursor()
    c.execute(
        "INSERT INTO transactions(user_id,type,amount,category,note,date) VALUES (?,?,?,?,?,?)",
        (str(user_id), ttype, float(amount), category, note, date)
    )
    conn.commit()

def learn_category(word: str, category: str):
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO categories_learned(word, category) VALUES (?, ?)", (word.lower(), category))
    conn.commit()

def get_learned_category(word: str):
    c = conn.cursor()
    c.execute("SELECT category FROM categories_learned WHERE word=?", (word.lower(),))
    r = c.fetchone()
    return r[0] if r else None

def get_balance(user_id: str) -> float:
    c = conn.cursor()
    c.execute("SELECT SUM(amount) FROM transactions WHERE user_id=?", (str(user_id),))
    r = c.fetchone()[0]
    return float(r or 0.0)

def get_total_expense(user_id: str, period: str = None) -> float:
    c = conn.cursor()
    query = "SELECT SUM(amount) FROM transactions WHERE user_id=? AND amount<0"
    params = [str(user_id)]
    if period:
        query += " AND date>=?"
        params.append(period)
    c.execute(query, tuple(params))
    r = c.fetchone()[0]
    return abs(r or 0.0)

def get_total_income(user_id: str, period: str = None) -> float:
    c = conn.cursor()
    query = "SELECT SUM(amount) FROM transactions WHERE user_id=? AND amount>0"
    params = [str(user_id)]
    if period:
        query += " AND date>=?"
        params.append(period)
    c.execute(query, tuple(params))
    r = c.fetchone()[0]
    return float(r or 0.0)

def get_transactions(user_id: str, period: str = None):
    c = conn.cursor()
    query = "SELECT date,amount,category,note FROM transactions WHERE user_id=?"
    params = [str(user_id)]
    if period:
        query += " AND date>=?"
        params.append(period)
    query += " ORDER BY date DESC"
    c.execute(query, tuple(params))
    return c.fetchall()

def generate_csv_bytes(user_id: str, period: str = None, monthly: bool = False):
    rows = get_transactions(user_id, period)
    if not rows:
        return None
    # Use BytesIO e BOM utf-8-sig para compatibilidade Excel/Windows
    bio = io.BytesIO()
    writer = csv.writer(io.TextIOWrapper(bio, encoding='utf-8-sig', newline=''), quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["date", "amount", "category", "note"])
    for r in rows:
        writer.writerow(list(r))
    # flush wrapper and reset buffer
    bio.seek(0)
    filename = "transacoes_mes_atual.csv" if monthly else "transacoes_totais.csv"
    return bio, filename

# ===== UTILIDADES (valor/currency parsing) =====
def normalize_number_token(token: str) -> str:
    # Remove currency symbols and spaces, keep digits, commas, dots and minus
    return re.sub(r"[^\d,.\-]", "", token)

def find_number_matches(text: str) -> List[Tuple[str, int, int]]:
    """
    Retorna lista de (raw_number_string, start_index, end_index)
    Aceita formatos como: 12,50  12.50  R$12,50  -50
    """
    # procura padrões com ou sem R$, com , ou .
    pattern = re.compile(r"(?:r\$|\$)?\s*-?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\b-?\d+[.,]?\d*\b", flags=re.IGNORECASE)
    matches = []
    for m in pattern.finditer(text):
        raw = m.group(0)
        raw_norm = normalize_number_token(raw)
        if raw_norm == "" or raw_norm == "-" or raw_norm == "." or raw_norm == ",":
            continue
        matches.append((raw_norm, m.start(), m.end()))
    return matches

# ===== INTERPRETAÇÃO INTELIGENTE V2 (múltiplas transações) =====
def interpret_text_v2(text: str) -> List[Tuple[float, str, str, str]]:
    """
    Retorna lista de (amount, ttype, category, note)
    """
    text_clean = text.lower()
    number_matches = find_number_matches(text_clean)
    if not number_matches:
        return []

    expense_words = ["gastei","paguei","paguei","pago","gasto","comprei","compras","gasto"]
    income_words = ["recebi","ganhei","salário","salario","bônus","bonus","entrada","ganho","deposito","depósito"]

    results = []
    words = re.split(r"\s+", text_clean)

    # para cada ocorrência de número, cria contexto
    for raw_num, start_idx, end_idx in number_matches:
        # converte para float, suportando , como decimal
        try:
            # remover milhar (p.ex. 1.234,56 ou 1,234.56)
            num = raw_num
            # Se tem both '.' and ',', decidir qual é decimal: se último separador é ',' assume decimal separator ','
            if num.count(",") and num.count("."):
                if num.rfind(",") > num.rfind("."):
                    num = num.replace(".", "").replace(",", ".")
                else:
                    num = num.replace(",", "")
            else:
                num = num.replace(",", ".")
            amount = float(num)
        except Exception:
            continue

        # cria janela de contexto textual ao redor do número (50 caracteres)
        ctx_start = max(0, start_idx - 40)
        ctx_end = min(len(text_clean), end_idx + 40)
        window_text = text_clean[ctx_start:ctx_end]

        # tipo
        ttype = None
        if any(w in window_text for w in expense_words):
            ttype = "expense"
            amount = -abs(amount)
        elif any(w in window_text for w in income_words):
            ttype = "income"
            amount = abs(amount)
        else:
            # fallback: sinal do número
            ttype = "income" if amount >= 0 else "expense"

        # tenta achar palavra-chave mais próxima (pegar a palavra à esquerda/ direita do número)
        # tokeniza por palavras e posiciona
        left_portion = text_clean[:start_idx]
        right_portion = text_clean[end_idx:]
        left_words = re.findall(r"\w+", left_portion)
        right_words = re.findall(r"\w+", right_portion)
        nearest_words = []
        if left_words:
            nearest_words.extend(left_words[-3:][::-1])  # palavras mais próximas à esquerda (ordenadas mais próximo primeiro)
        if right_words:
            nearest_words.extend(right_words[:3])  # palavras à direita

        # procura categoria aprendida nas palavras próximas
        category = None
        for w in nearest_words:
            if w in STOPWORDS or re.match(r"-?\d+[.,]?\d*", w):
                continue
            learned = get_learned_category(w)
            if learned:
                category = learned
                break

        # procura categorias padrão por keywords no window_text
        if not category:
            pool = CATEGORIES_EXPENSE.items() if ttype == "expense" else CATEGORIES_INCOME.items()
            for cat, keywords in pool:
                if any(kw in window_text for kw in keywords):
                    category = cat
                    break

        if not category:
            category = "sugerir"

        # nota: extrair trecho útil (remover só o número)
        # remove a primeira ocorrência do raw num no trecho para não repetir
        raw_repr = raw_num
        note = (text_clean[:ctx_start] + text_clean[ctx_start:ctx_end].replace(raw_repr, "") + text_clean[ctx_end:]).strip()
        # compactar espaços
        note = re.sub(r"\s+", " ", note).strip()
        results.append((amount, ttype, category, note))

    return results

# ===== TECLADO PRINCIPAL =====
def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("Adicionar despesa", callback_data="add_expense"),
         InlineKeyboardButton("Adicionar receita", callback_data="add_income")],
        [InlineKeyboardButton("Resetar conta", callback_data="reset_account"),
         InlineKeyboardButton("Saldo (valor atual)", callback_data="show_balance")],
        [InlineKeyboardButton("Valor gasto", callback_data="show_expense"),
         InlineKeyboardButton("Exportar CSV", callback_data="export_csv")],
        [InlineKeyboardButton("Relatório mensal", callback_data="monthly_report")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ===== UTIL: edição segura (evita BadRequest) =====
async def safe_edit_or_reply(query: telegram.CallbackQuery, text: str, reply_markup=None):
    """
    Tenta editar a mensagem do callback query; se não for possível (sem texto original,
    ou texto igual), envia uma nova mensagem.
    """
    try:
        # se a mensagem original não tem texto ou é None, enviar reply
        original_text = None
        if query.message and query.message.text:
            original_text = query.message.text
        if original_text and original_text != text:
            await query.edit_message_text(text, reply_markup=reply_markup)
        else:
            # envia nova mensagem (reply) para o chat
            await query.message.reply_text(text, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        # caso especial: "Message is not modified" ou "There is no text in the message to edit"
        try:
            await query.message.reply_text(text, reply_markup=reply_markup)
        except Exception:
            logger.exception("Erro ao enviar mensagem de fallback.")

# ===== HANDLER DE MENSAGEM =====
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # texto do usuário
    text = update.message.text.strip()
    user_id = update.effective_user.id
    text_lower = text.lower()

    # saudações
    if any(g in text_lower for g in ["oi", "olá", "ola", "bom dia", "boa tarde", "boa noite"]):
        await update.message.reply_text("Olá! Eu sou seu bot de controle financeiro.", reply_markup=get_main_keyboard())
        return

    # interpretar múltiplas transações
    transactions = interpret_text_v2(text)
    if not transactions:
        await update.message.reply_text(
            "Não consegui identificar transações. Tente algo como 'Gastei 50 no mercado' ou 'Recebi 500 salário'.",
            reply_markup=get_main_keyboard()
        )
        return

    # processa cada transação detectada
    inserted = 0
    for amount, ttype, category, note in transactions:
        if category == "sugerir":
            # salva pendente para quando o usuário escolher a categoria
            context.user_data["pending_transaction"] = (amount, ttype, note)
            keyboard = [
                [InlineKeyboardButton("Alimentação", callback_data="cat_alimentacao")],
                [InlineKeyboardButton("Transporte", callback_data="cat_transporte")],
                [InlineKeyboardButton("Salário", callback_data="cat_salario")],
                [InlineKeyboardButton("Cartão de Crédito", callback_data="cat_cartao")],
                [InlineKeyboardButton("Outros", callback_data="cat_outros")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(f"Escolha a categoria para: {amount:.2f}", reply_markup=reply_markup)
        else:
            add_transaction(user_id, amount, category, note)
            inserted += 1

    if inserted > 0:
        bal = get_balance(user_id)
        await update.message.reply_text(f"{inserted} transação(ões) registradas.\nSaldo atual: {bal:.2f}", reply_markup=get_main_keyboard())

# ===== HANDLER DE BOTÕES =====
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    # confirmar categoria pendente
    transaction = context.user_data.get("pending_transaction")
    if data.startswith("cat_") and transaction:
        amount, ttype, note = transaction
        category_map = {
            "cat_alimentacao": "alimentação",
            "cat_transporte": "transporte",
            "cat_salario": "salário",
            "cat_cartao": "cartão de crédito",
            "cat_outros": "outros"
        }
        category = category_map.get(data, "outros")
        add_transaction(user_id, amount, category, note)
        # aprende palavras úteis da nota (não aprende stopwords nem números)
        for word in re.findall(r"\w+", note):
            if word not in STOPWORDS and not re.match(r"-?\d+[.,]?\d*", word):
                learn_category(word, category)
        bal = get_balance(user_id)
        tipo_texto = "Despesa" if ttype == "expense" else "Receita"
        context.user_data.pop("pending_transaction", None)
        await safe_edit_or_reply(query, f"{tipo_texto} registrada: {abs(amount):.2f} | {category}\nSaldo atual: {bal:.2f}", reply_markup=get_main_keyboard())
        return

    # ações gerais
    if data == "add_expense":
        await safe_edit_or_reply(query, "Envie sua despesa no formato livre, ex: 'gastei 50 no mercado'", reply_markup=get_main_keyboard())
    elif data == "add_income":
        await safe_edit_or_reply(query, "Envie sua receita no formato livre, ex: 'recebi 5000 salário'", reply_markup=get_main_keyboard())
    elif data == "reset_account":
        c = conn.cursor()
        c.execute("DELETE FROM transactions WHERE user_id=?", (str(user_id),))
        conn.commit()
        await safe_edit_or_reply(query, "Todas as suas transações foram apagadas. Saldo zerado.", reply_markup=get_main_keyboard())
    elif data == "show_balance":
        bal = get_balance(user_id)
        await safe_edit_or_reply(query, f"Saldo atual: {bal:.2f}", reply_markup=get_main_keyboard())
    elif data == "show_expense":
        total_expense = get_total_expense(user_id)
        await safe_edit_or_reply(query, f"Valor total gasto: {total_expense:.2f}", reply_markup=get_main_keyboard())
    elif data == "export_csv":
        csv_bytes = generate_csv_bytes(user_id)
        if not csv_bytes:
            await safe_edit_or_reply(query, "Nenhuma transação registrada para exportar.", reply_markup=get_main_keyboard())
            return
        bio, filename = csv_bytes
        bio.seek(0)
        await query.message.reply_document(document=InputFile(bio, filename=filename), reply_markup=get_main_keyboard())
    elif data == "monthly_report":
        now = datetime.now(timezone.utc)
        period_iso = datetime(now.year, now.month, 1, tzinfo=timezone.utc).isoformat()
        bal = get_balance(user_id)
        total_income = get_total_income(user_id, period_iso)
        total_expense = get_total_expense(user_id, period_iso)
        transactions = get_transactions(user_id, period_iso)
        count = len(transactions)
        reply_text = (
            f"📊 Relatório mensal\n"
            f"Saldo atual: {bal:.2f}\n"
            f"Receitas: {total_income:.2f}\n"
            f"Despesas: {total_expense:.2f}\n"
            f"Número de transações: {count}"
        )
        await safe_edit_or_reply(query, reply_text, reply_markup=get_main_keyboard())
        # envia CSV do mês atual (se houver)
        if transactions:
            bio, filename = generate_csv_bytes(user_id, period=period_iso, monthly=True)
            bio.seek(0)
            await query.message.reply_document(document=InputFile(bio, filename=filename))

# ===== MAIN =====
def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("Bot rodando...")
    app.run_polling()

if __name__ == "__main__":
    main()
