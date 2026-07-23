# صورة جاهزة من فريق Scrapling نفسه، فيها كل مكتبات النظام والمتصفحات
# اللي محتاجينها من الأول (بتحل مشكلة "shared object file" نهائياً)
FROM ghcr.io/d4vinci/scrapling:latest

WORKDIR /app

# ننسخ ملفات المشروع
COPY main.py /app/main.py
COPY requirements.txt /app/requirements.txt

# نتأكد إن مكتبة تليجرام متثبتة (Scrapling أصلاً متثبتة في الصورة دي)
RUN pip install --no-cache-dir -r requirements.txt

CMD ["python3", "main.py"]
