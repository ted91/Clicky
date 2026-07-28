#ifndef AUDIO_BSP_H
#define AUDIO_BSP_H


#ifdef __cplusplus
extern "C" {
#endif

void audio_bsp_init(void);
void i2s_music(void *args);
void i2s_echo(void *arg);
void audio_playback_set_vol(uint8_t vol);
uint8_t *i2s_get_handle(uint32_t *len);

void audio_play_init(void);

void audio_playback_read(void *data_ptr,uint32_t len);

void audio_playback_write(void *data_ptr,uint32_t len);

// Battery: close/reopen both codec channels around actual use (recording,
// click sounds) -- see audio_bsp.c. Idempotent; power_up restores the same
// volume/gain/sample format audio_play_init sets.
void audio_bsp_power_down(void);
void audio_bsp_power_up(void);
int audio_bsp_is_powered(void);

#ifdef __cplusplus
}
#endif



#endif