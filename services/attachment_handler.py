import base64
import io
from PIL import Image
import logging
import os
import mimetypes

from config import Config

class AttachmentHandler:
    """
    Handles processing, compression, and management of file attachments.
    """
    
    def __init__(self, max_size_bytes=1024*1024):  # Default 1MB max size
        """
        Initialize the attachment handler.
        
        Args:
            max_size_bytes: Maximum size for attachments (default: 1MB)
        """
        self.max_size_bytes = max_size_bytes
        self.temp_dir = os.path.join(Config.DATA_DIR, 'temp_attachments')
        
        # Ensure temp directory exists
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)
    
    def process_incoming_attachment(self, data, filename, mime_type):
        """
        Process an incoming attachment from XMTP.

        Args:
            data: Binary attachment data (bytes or list of integers from JS)
            filename: Original filename
            mime_type: MIME type of the attachment

        Returns:
            dict: Processed attachment info with data, filename, and mime_type
        """
        logging.info(f"Processing attachment: {filename} ({mime_type})")

        # Convert list of integers (from JS Array.from(Uint8Array)) to bytes
        if isinstance(data, list):
            data = bytes(data)

        # If attachment is already small enough, return as is
        if len(data) <= self.max_size_bytes:
            return {
                'data': data,
                'filename': filename,
                'mimeType': mime_type
            }
        
        # Check if this is an image
        if mime_type.startswith('image/'):
            # Compress the image
            try:
                compressed_data = self._compress_image(data, mime_type)
                
                # If compression was successful
                if compressed_data and len(compressed_data) <= self.max_size_bytes:
                    logging.info(f"Successfully compressed image from {len(data)} bytes to {len(compressed_data)} bytes")
                    return {
                        'data': compressed_data,
                        'filename': filename,
                        'mimeType': mime_type
                    }
            except Exception as e:
                logging.error(f"Error compressing image: {e}")

            # Still too big in its own format (a PNG stays lossless, so a 1 MP
            # screenshot can stay over the cap): fall back to JPEG, which every
            # image survives.
            if mime_type.lower() not in ('image/jpeg', 'image/jpg'):
                try:
                    jpeg_data = self._compress_image(data, 'image/jpeg')
                    if jpeg_data and len(jpeg_data) <= self.max_size_bytes:
                        logging.info(f"Re-encoded {mime_type} as JPEG: {len(data)} -> {len(jpeg_data)} bytes")
                        base = filename.rsplit('.', 1)[0] if '.' in filename else filename
                        return {
                            'data': jpeg_data,
                            'filename': f"{base}.jpg",
                            'mimeType': 'image/jpeg'
                        }
                except Exception as e:
                    logging.error(f"Error re-encoding image as JPEG: {e}")

        # If we reach here, the attachment is too large and not compressible
        logging.warning(f"Attachment too large ({len(data)} bytes) and could not be compressed")
        return {
            'data': None,
            'filename': filename,
            'mimeType': mime_type,
            'error': 'Attachment too large'
        }
    
    def _compress_image(self, image_data, mime_type, quality=85):
        """
        Compress an image to reduce its size.
        
        Args:
            image_data: Binary image data
            mime_type: MIME type of the image
            quality: JPEG compression quality (0-100)
            
        Returns:
            bytes: Compressed image data
        """
        try:
            # Convert mime_type to PIL format
            format_map = {
                'image/jpeg': 'JPEG',
                'image/jpg': 'JPEG',
                'image/png': 'PNG',
                'image/gif': 'GIF',
                'image/webp': 'WEBP'
            }
            
            pil_format = format_map.get(mime_type.lower(), 'JPEG')
            
            # Open image from binary data
            img = Image.open(io.BytesIO(image_data))
            
            # Calculate new dimensions (if needed)
            curr_width, curr_height = img.size
            target_pixels = 1000000  # Target ~1 megapixel for large images
            
            # Only resize if the image is very large
            if (curr_width * curr_height) > target_pixels:
                ratio = (target_pixels / (curr_width * curr_height)) ** 0.5
                new_width = int(curr_width * ratio)
                new_height = int(curr_height * ratio)
                img = img.resize((new_width, new_height), Image.LANCZOS)
            
            # Save to bytes with compression
            output = io.BytesIO()
            
            if pil_format == 'JPEG':
                img = img.convert('RGB')  # Remove alpha channel for JPEG
                img.save(output, format=pil_format, quality=quality, optimize=True)
            elif pil_format == 'PNG':
                img.save(output, format=pil_format, optimize=True)
            else:
                img.save(output, format=pil_format)
            
            compressed_data = output.getvalue()
            
            # If still too large, try more aggressive compression
            if len(compressed_data) > self.max_size_bytes and pil_format == 'JPEG':
                output = io.BytesIO()
                img.save(output, format='JPEG', quality=60, optimize=True)
                compressed_data = output.getvalue()
            
            return compressed_data
            
        except Exception as e:
            logging.error(f"Error in image compression: {e}")
            return None
    
    def process_outgoing_attachment(self, filepath=None, data=None, filename=None, mime_type=None):
        """
        Process an attachment for sending via XMTP.
        
        Args:
            filepath: Path to the file (optional)
            data: Binary data (optional, alternative to filepath)
            filename: Custom filename (required if data is provided)
            mime_type: Custom MIME type (optional)
            
        Returns:
            dict: Attachment information for sending
        """
        try:
            # Get file data either from filepath or provided data
            if filepath:
                with open(filepath, 'rb') as f:
                    file_data = f.read()
                
                # Get filename from path if not provided
                if not filename:
                    filename = os.path.basename(filepath)
                
                # Guess MIME type if not provided
                if not mime_type:
                    mime_type, _ = mimetypes.guess_type(filepath)
                    if not mime_type:
                        mime_type = 'application/octet-stream'
            
            elif data:
                # Use provided data
                if not filename:
                    raise ValueError("Filename must be provided when using binary data")
                
                file_data = data
                
                # Guess MIME type from filename if not provided
                if not mime_type:
                    mime_type, _ = mimetypes.guess_type(filename)
                    if not mime_type:
                        mime_type = 'application/octet-stream'
            
            else:
                raise ValueError("Either filepath or data must be provided")
            
            # Process and potentially compress the attachment
            processed = self.process_incoming_attachment(file_data, filename, mime_type)
            
            # Check if processing was successful
            if processed.get('error'):
                return {'error': processed['error']}
            
            return {
                'data': processed['data'],
                'filename': processed['filename'],
                'mimeType': processed['mimeType']
            }
            
        except Exception as e:
            logging.error(f"Error processing outgoing attachment: {e}")
            return {'error': str(e)}
    
    def store_temp_attachment(self, data, filename):
        """
        Store an attachment temporarily on disk.
        
        Args:
            data: Binary attachment data
            filename: Filename to use
            
        Returns:
            str: Path to the stored file
        """
        filepath = os.path.join(self.temp_dir, filename)
        
        try:
            with open(filepath, 'wb') as f:
                f.write(data)
            
            return filepath
        except Exception as e:
            logging.error(f"Error storing attachment: {e}")
            return None
    
    def cleanup_temp_files(self):
        """Clean up temporary attachment files"""
        for filename in os.listdir(self.temp_dir):
            filepath = os.path.join(self.temp_dir, filename)
            try:
                if os.path.isfile(filepath):
                    os.unlink(filepath)
            except Exception as e:
                logging.error(f"Error deleting {filepath}: {e}")