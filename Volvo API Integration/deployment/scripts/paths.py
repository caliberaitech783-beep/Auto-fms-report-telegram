import os
HERE   = os.path.dirname(os.path.abspath(__file__))
BASE   = os.path.dirname(HERE)
CONFIG = os.path.join(BASE, "config")
REPORTS= os.path.join(BASE, "reports")
PDF_OUTBOX = os.path.join(REPORTS, "WhatsApp_Pending")
LOGS   = os.path.join(BASE, "logs")
