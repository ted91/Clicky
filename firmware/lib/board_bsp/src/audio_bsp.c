#include <stdio.h>
#include "audio_bsp.h"
#include "freertos/FreeRTOS.h"
#include "src/codec_board/codec_board.h"
#include "src/codec_board/codec_init.h"
#include "src/esp_codec_dev/include/esp_codec_dev.h"
#include "esp_heap_caps.h"


esp_codec_dev_handle_t playback = NULL;
esp_codec_dev_handle_t record = NULL;


extern const uint8_t music_pcm_start[] asm("_binary_canon_pcm_start");
extern const uint8_t music_pcm_end[]   asm("_binary_canon_pcm_end");


void audio_bsp_init(void)
{
  set_codec_board_type("S3_ePaper_1_54");
	codec_init_cfg_t codec_cfg = 
  {
    .in_mode = CODEC_I2S_MODE_STD,
    .out_mode = CODEC_I2S_MODE_STD,
    .in_use_tdm = false,
    .reuse_dev = false,
  };
  ESP_ERROR_CHECK(init_codec(&codec_cfg));
  playback = get_playback_handle();
  record = get_record_handle();
}

void i2s_music(void *args)
{
  esp_codec_dev_set_out_vol(playback, 80.0);  //设置80声音大小
  for(;;)
  {
    size_t bytes_write = 0;
    size_t bytes_sizt = music_pcm_end - music_pcm_start;
    uint8_t *data_ptr = (uint8_t *)music_pcm_start;
    esp_codec_dev_sample_info_t fs = {};
      fs.sample_rate = 16000;
      fs.channel = 2;
      fs.bits_per_sample = 16;
    if(esp_codec_dev_open(playback, &fs) == ESP_CODEC_DEV_OK)
    {
      while (bytes_write < bytes_sizt)
      {
        esp_codec_dev_write(playback, data_ptr, 256);
        data_ptr += 256;
        bytes_write += 256;
      }
      //esp_codec_dev_close(playback); //close //关闭两个通道的,播放和录音
    }
    else
    {
      break;
    }
  }
  vTaskDelete(NULL);
}
void i2s_echo(void *arg)
{
  	esp_codec_dev_set_out_vol(playback, 60.0); //设置100声音大小
  	esp_codec_dev_set_in_gain(record, 15.0);   //设置录音时的增益
  	uint8_t *data_ptr = (uint8_t *)heap_caps_malloc(1024 * sizeof(uint8_t), MALLOC_CAP_SPIRAM);
  	esp_codec_dev_sample_info_t fs = {};
  	  fs.sample_rate = 48000;
  	  fs.channel = 2;
  	  fs.bits_per_sample = 16;
  	esp_codec_dev_open(playback, &fs); //打开播放
  	esp_codec_dev_open(record, &fs);   //打开录音
  	for(;;)
  	{
  	  	if(ESP_CODEC_DEV_OK == esp_codec_dev_read(record, data_ptr, 1024))
  	  	{
  	  	  	esp_codec_dev_write(playback, data_ptr, 1024);
  	  	}
  	}
}

void audio_playback_set_vol(uint8_t vol)
{
  	esp_codec_dev_set_out_vol(playback, vol); //设置60声音大小
}

// Input gain for the record channel. The original vendor/reference value
// here was 45.0dB -- very hot, and a likely clipping source for anything
// louder than a quiet room (a normal conversation, let alone a noisy
// venue). Raised from 20.0 to 30.0 -- 20.0 avoided clipping but real-world
// diarization results showed a second, quieter speaker's short backchannel
// words ("yeah", "mhmm") landing near the noise floor and getting
// mis-transcribed/misattributed by the STT provider's diarization. 30.0 is
// a judgment-call compromise (roughly midway between 20 and the original
// 45), not a measured value -- still needs a real on-device conversation
// test to confirm it actually helps without reintroducing clipping on the
// primary/loud speaker. Clipping matters beyond just audio quality:
// audio_analysis.py's RMS-based primary/background classification (see its
// module docstring) depends on a loud segment's measured level actually
// reflecting how loud it was -- clipping compresses exactly that.
#define RECORD_IN_GAIN_DB 30.0

void audio_play_init(void)
{
	esp_codec_dev_set_out_vol(playback, 100.0); //设置100声音大小
  	esp_codec_dev_set_in_gain(record, RECORD_IN_GAIN_DB);
  	esp_codec_dev_sample_info_t fs = {};
  	  fs.sample_rate = 16000;
  	  fs.channel = 2;
  	  fs.bits_per_sample = 16;
  	esp_codec_dev_open(playback, &fs); //打开播放
  	esp_codec_dev_open(record, &fs);   //打开录音
}

// Battery: the codec draws several mA continuously while its channels are
// open, and historically they were opened once at boot and never closed
// (the commented-out close in i2s_music above). These let recorder.cpp
// close both channels whenever nothing is recording or playing a click,
// and reopen them (same sample format as audio_play_init) on demand.
//
// Deliberately does NOT cut the Audio power rail (GPIO42): losing the rail
// would require a full codec re-init (I2C register reprogramming) on every
// recording start, and a failure there would break recording outright --
// closing the esp_codec_dev channels already puts the codec chips into
// their driver-managed low-power state, which is most of the win at none
// of the risk.
static int s_audio_powered = 1; // channels start open via audio_play_init

void audio_bsp_power_down(void)
{
	if (!s_audio_powered) return;
	esp_codec_dev_close(playback);
	esp_codec_dev_close(record);
	s_audio_powered = 0;
}

void audio_bsp_power_up(void)
{
	if (s_audio_powered) return;
	esp_codec_dev_sample_info_t fs = {};
	  fs.sample_rate = 16000;
	  fs.channel = 2;
	  fs.bits_per_sample = 16;
	esp_codec_dev_open(playback, &fs);
	esp_codec_dev_open(record, &fs);
	esp_codec_dev_set_out_vol(playback, 100.0);
	esp_codec_dev_set_in_gain(record, RECORD_IN_GAIN_DB); // keep in sync with audio_play_init()
	s_audio_powered = 1;
}

int audio_bsp_is_powered(void)
{
	return s_audio_powered;
}

void audio_playback_read(void *data_ptr,uint32_t len)
{
	esp_codec_dev_read(record, data_ptr, len);
}

void audio_playback_write(void *data_ptr,uint32_t len)
{
	esp_codec_dev_write(playback, data_ptr, len);
}

uint8_t *i2s_get_handle(uint32_t *len)
{
    size_t bytes_sizt = music_pcm_end - music_pcm_start;
    uint8_t *data_ptr = (uint8_t *)music_pcm_start;
    *len = bytes_sizt;
    return data_ptr;
}