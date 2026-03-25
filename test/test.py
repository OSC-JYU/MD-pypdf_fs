from pypdf import PdfReader, PdfWriter
import os
import time
import tempfile

def fix_broken_pdf(input_path):
    """Fix a broken PDF by finding the first %PDF- marker and extracting from there."""
    with open(input_path, 'rb') as f:
        content = f.read()
    
    # Find the first occurrence of %PDF-
    pdf_marker = b'%PDF-'
    marker_pos = content.find(pdf_marker)
    
    if marker_pos == -1:
        raise ValueError("No PDF marker found in file")
    
    # Extract everything from the marker onwards
    fixed_content = content[marker_pos:]
    
    # Write to a temporary file
    temp_fd, temp_path = tempfile.mkstemp(suffix='.pdf')
    try:
        with os.fdopen(temp_fd, 'wb') as f:
            f.write(fixed_content)
        return temp_path
    except Exception:
        os.close(temp_fd)
        raise

def split_pdf(input_path, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    
    # Try to read the PDF, fix if it fails
    temp_file = None
    try:
        reader = PdfReader(input_path)
        # Test if we can access pages
        _ = len(reader.pages)
    except Exception:
        # PDF is broken, try to fix it
        print(f"PDF read failed, attempting to fix: {input_path}")
        temp_file = fix_broken_pdf(input_path)
        reader = PdfReader(temp_file)
    
    try:
        for i, page in enumerate(reader.pages):
            writer = PdfWriter()
            writer.add_page(page)
            output_path = f"{output_dir}/page_{i + 1}.pdf"
            with open(output_path, "wb") as f:
                writer.write(f)
    finally:
        # Clean up temporary file if it was created
        if temp_file and os.path.exists(temp_file):
            os.remove(temp_file)

# Example usage
start_time = time.time()
split_pdf("broken.pdf", "./output_pages")
end_time = time.time()
elapsed_time = end_time - start_time
print(f"Execution time: {elapsed_time:.4f} seconds")

