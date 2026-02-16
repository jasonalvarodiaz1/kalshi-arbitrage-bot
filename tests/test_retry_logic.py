"""Unit tests for retry logic in KalshiAPI."""

import unittest
import sys
import os
from unittest.mock import Mock, patch, MagicMock
import requests

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_bot import KalshiAPI


class TestRetryLogic(unittest.TestCase):
    """Test retry logic in KalshiAPI._request_with_retry."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.api = KalshiAPI()
    
    def test_successful_request_no_retry(self):
        """Test that successful requests don't retry."""
        with patch.object(self.api.session, 'request') as mock_request:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {'data': 'test'}
            mock_request.return_value = mock_response
            
            result = self.api._request_with_retry('GET', 'https://api.test.com/test')
            
            # Should only call once for successful request
            self.assertEqual(mock_request.call_count, 1)
            self.assertEqual(result.status_code, 200)
    
    def test_retry_on_429(self):
        """Test retry on 429 rate limit."""
        with patch.object(self.api.session, 'request') as mock_request:
            # First call returns 429, second returns 200
            mock_response_429 = Mock()
            mock_response_429.status_code = 429
            mock_response_429.headers = {}
            
            mock_response_200 = Mock()
            mock_response_200.status_code = 200
            
            mock_request.side_effect = [mock_response_429, mock_response_200]
            
            with patch('time.sleep'):  # Mock sleep to speed up test
                result = self.api._request_with_retry('GET', 'https://api.test.com/test')
            
            # Should call twice (initial + 1 retry)
            self.assertEqual(mock_request.call_count, 2)
            self.assertEqual(result.status_code, 200)
    
    def test_retry_on_500(self):
        """Test retry on 500 server error."""
        with patch.object(self.api.session, 'request') as mock_request:
            # First call returns 500, second returns 200
            mock_response_500 = Mock()
            mock_response_500.status_code = 500
            mock_response_500.headers = {}
            
            mock_response_200 = Mock()
            mock_response_200.status_code = 200
            mock_response_200.headers = {}
            
            mock_request.side_effect = [mock_response_500, mock_response_200]
            
            with patch('time.sleep'):
                result = self.api._request_with_retry('GET', 'https://api.test.com/test')
            
            self.assertEqual(mock_request.call_count, 2)
            self.assertEqual(result.status_code, 200)
    
    def test_no_retry_on_401(self):
        """Test that 401 errors don't trigger retry."""
        with patch.object(self.api.session, 'request') as mock_request:
            mock_response = Mock()
            mock_response.status_code = 401
            mock_request.return_value = mock_response
            
            result = self.api._request_with_retry('GET', 'https://api.test.com/test')
            
            # Should only call once (no retry on 401)
            self.assertEqual(mock_request.call_count, 1)
            self.assertEqual(result.status_code, 401)
    
    def test_no_retry_on_404(self):
        """Test that 404 errors don't trigger retry."""
        with patch.object(self.api.session, 'request') as mock_request:
            mock_response = Mock()
            mock_response.status_code = 404
            mock_request.return_value = mock_response
            
            result = self.api._request_with_retry('GET', 'https://api.test.com/test')
            
            # Should only call once (no retry on 404)
            self.assertEqual(mock_request.call_count, 1)
            self.assertEqual(result.status_code, 404)
    
    def test_retry_exhaustion(self):
        """Test that retries are exhausted after max attempts."""
        with patch.object(self.api.session, 'request') as mock_request:
            # All calls return 500
            mock_response = Mock()
            mock_response.status_code = 500
            mock_response.headers = {}
            mock_request.return_value = mock_response
            
            with patch('time.sleep'):
                result = self.api._request_with_retry('GET', 'https://api.test.com/test', max_retries=3)
            
            # Should call 4 times (initial + 3 retries)
            self.assertEqual(mock_request.call_count, 4)
            self.assertEqual(result.status_code, 500)
    
    def test_retry_on_connection_error(self):
        """Test retry on connection error."""
        with patch.object(self.api.session, 'request') as mock_request:
            # First call raises ConnectionError, second returns 200
            mock_response = Mock()
            mock_response.status_code = 200
            
            mock_request.side_effect = [
                requests.exceptions.ConnectionError("Connection failed"),
                mock_response
            ]
            
            with patch('time.sleep'):
                result = self.api._request_with_retry('GET', 'https://api.test.com/test')
            
            self.assertEqual(mock_request.call_count, 2)
            self.assertEqual(result.status_code, 200)
    
    def test_retry_on_timeout(self):
        """Test retry on timeout error."""
        with patch.object(self.api.session, 'request') as mock_request:
            mock_response = Mock()
            mock_response.status_code = 200
            
            mock_request.side_effect = [
                requests.exceptions.Timeout("Request timed out"),
                mock_response
            ]
            
            with patch('time.sleep'):
                result = self.api._request_with_retry('GET', 'https://api.test.com/test')
            
            self.assertEqual(mock_request.call_count, 2)
            self.assertEqual(result.status_code, 200)
    
    def test_retry_after_header(self):
        """Test that Retry-After header is respected."""
        with patch.object(self.api.session, 'request') as mock_request:
            mock_response_429 = Mock()
            mock_response_429.status_code = 429
            mock_response_429.headers = {'Retry-After': '5'}
            
            mock_response_200 = Mock()
            mock_response_200.status_code = 200
            
            mock_request.side_effect = [mock_response_429, mock_response_200]
            
            with patch('time.sleep') as mock_sleep:
                result = self.api._request_with_retry('GET', 'https://api.test.com/test')
                
                # Check that sleep was called with the Retry-After value
                mock_sleep.assert_called_once_with(5)
            
            self.assertEqual(result.status_code, 200)
    
    def test_connection_error_exhaustion(self):
        """Test that connection errors return None after exhaustion."""
        with patch.object(self.api.session, 'request') as mock_request:
            mock_request.side_effect = requests.exceptions.ConnectionError("Connection failed")
            
            with patch('time.sleep'):
                result = self.api._request_with_retry('GET', 'https://api.test.com/test', max_retries=2)
            
            # Should call 3 times (initial + 2 retries)
            self.assertEqual(mock_request.call_count, 3)
            self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()
