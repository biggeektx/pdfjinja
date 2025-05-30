# -*- coding: utf-8 -*-
""" Example python script that fills out a PDF based on Jinja templates. """

import os
import sys
from pdfjinja import PdfJinja

def main():
    if len(sys.argv) < 2:
        print("Usage: python example.py <file_path> [cobuyer]")
        sys.exit(1)

    file_path = sys.argv[1]
    include_cobuyer = len(sys.argv) > 2 and sys.argv[2].lower() == 'cobuyer'

    dirname = os.path.dirname(__file__)
    template_pdf = PdfJinja(file_path)

    context = {
        'sig': os.path.join(dirname, 'Buyer_X.png')
    }

    if include_cobuyer:
        context['sig2'] = os.path.join(dirname, 'CoBuyer_X.png')

    rendered_pdf = template_pdf(context)
    rendered_pdf.write(open(file_path, 'wb'))

if __name__ == "__main__":
    main()
