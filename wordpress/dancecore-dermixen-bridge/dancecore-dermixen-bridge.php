<?php
/**
 * Plugin Name: Dancecore Dermixen Bridge
 * Description: Site-side job queue for the external Dermixen container. Audio is never mixed in PHP.
 * Version: 0.5.3
 * Requires PHP: 8.0
 */
defined('ABSPATH') || exit;
const DCMIX_BRIDGE_VERSION = '0.5.3';

function dcmix_table(): string {
    global $wpdb;
    return $wpdb->prefix . 'dcmix_jobs';
}
register_activation_hook(__FILE__, static function () {
    global $wpdb;
    require_once ABSPATH . 'wp-admin/includes/upgrade.php';
    $table = dcmix_table();
    $charset = $wpdb->get_charset_collate();
    dbDelta("CREATE TABLE $table (
       id varchar(36) NOT NULL,
       user_id bigint(20) unsigned NOT NULL,
       source_list_id bigint(20) unsigned NOT NULL DEFAULT 0,
       status varchar(16) NOT NULL DEFAULT 'queued',
       created_at datetime NOT NULL,
       started_at datetime NULL,
       finished_at datetime NULL,
       attempts int unsigned NOT NULL DEFAULT 0,
       request_json longtext NOT NULL,
       result_json longtext NULL,
       error_message text NULL,
       PRIMARY KEY  (id),
       KEY queue (status,created_at),
       KEY owner (user_id,created_at)
    ) $charset;");
    // The worker's random bearer token stays on the storage host.
    // WordPress stores only its SHA-256 verifier after admin pairing.
    if (!get_page_by_path('mix-generator')) {
        wp_insert_post([
          'post_type'=>'page','post_status'=>'publish',
          'post_title'=>'AI Mix Generator','post_name'=>'mix-generator',
          'post_content'=>'[dcmix_generator]'
        ],true);
    }
});

function dcmix_worker_auth(WP_REST_Request $request) {
    $header = $request->get_header('authorization') ?: '';
    if (!preg_match('/^Bearer ([a-f0-9]{64})$/D', $header, $matches)) {
        return new WP_Error('dcmix_auth', 'Worker authentication required', ['status'=>403]);
    }
    $expected_hash = (string)get_option('dcmix_worker_token_hash','');
    $presented_hash = hash('sha256',$matches[1]);
    return strlen($expected_hash)===64 && hash_equals($expected_hash,$presented_hash)
        ? true : new WP_Error('dcmix_auth','Worker authentication failed',['status'=>403]);
}
function dcmix_user_auth() {
    return is_user_logged_in()
      ? true : new WP_Error('dcmix_login','Log in to submit mixes',['status'=>401]);
}

function dcmix_source(int $post_id) {
    if (!$post_id || get_post_type($post_id)!=='track' || get_post_status($post_id)!=='publish'
        || post_password_required($post_id)) return null;
    if (trim((string)get_post_meta($post_id,'source_url',true))!=='') return null;
    $license = trim((string)get_post_meta($post_id,'license',true));
    // Only explicit CC0 zero-rights-reserved sources; never infer permission
    // from a hosted file or a permissive-sounding description.
    if (!preg_match('/(?:^|[^a-z0-9])cc0(?:$|[^a-z0-9])/i',$license)) return null;
    $aid=absint(get_post_meta($post_id,'file',true));
    if (!$aid || get_post_type($aid)!=='attachment') return null;
    $path=(string)get_post_meta($aid,'_wp_attached_file',true);
    if (!preg_match('~^20[0-9]{2}/(?:0[1-9]|1[0-2])/[a-zA-Z0-9_ .()+-]{1,190}\.(?:mp3|wav|flac|m4a|aac|ogg|opus)$~i',$path)) return null;
    $url=wp_get_attachment_url($aid);
    if (!$url || !str_starts_with($url,home_url('/dc-media/'))) return null;
    $sha=(string)get_post_meta($aid,'_dc_remote_sha256',true);
    if (!preg_match('/^[a-f0-9]{64}$/',$sha)) $sha=null;
    return ['track_id'=>$post_id,'media_path'=>$path,'sha256'=>$sha];
}
function dcmix_playlist_signature(int $list_id): string {
    global $wpdb;
    $ids=$wpdb->get_col($wpdb->prepare(
      "SELECT track_id FROM {$wpdb->prefix}dc_list_items
       WHERE list_id=%d ORDER BY sort_order ASC,id ASC LIMIT 500",$list_id));
    return hash('sha256',wp_json_encode(array_map('intval',$ids)));
}

function dcmix_collect_tracks(int $list_id, int $user_id) {
    global $wpdb;
    $list_table=$wpdb->prefix.'dc_lists';
    $items_table=$wpdb->prefix.'dc_list_items';
    $list=$wpdb->get_row($wpdb->prepare("SELECT id,user_id,visibility,title FROM $list_table WHERE id=%d",$list_id));
    if (!$list || ((string)$list->visibility!=='public' && (int)$list->user_id!==$user_id)) {
        return new WP_Error('dcmix_list','Playlist not accessible',['status'=>403]);
    }
    $ids=$wpdb->get_col($wpdb->prepare(
      "SELECT track_id FROM $items_table WHERE list_id=%d ORDER BY sort_order ASC,id ASC LIMIT 100",
      $list_id
    ));
    $tracks=[];$omitted=[];
    foreach($ids as $id) {
        $id=(int)$id;
        $source=dcmix_source($id);
        if ($source) $tracks[]=$source;
        else $omitted[]=$id;
    }
    if (count($tracks)<2) {
        return new WP_Error('dcmix_sources','Need at least two published site-hosted CC0 tracks in this playlist. External or unverified music cannot be mixed.',['status'=>422]);
    }
    if (count($tracks)>60) {
        return new WP_Error('dcmix_limit','Maximum 60 eligible tracks per mix',['status'=>422]);
    }
    return ['tracks'=>$tracks,'omitted'=>$omitted,'title'=>(string)$list->title];
}

add_action('rest_api_init',static function(){
    register_rest_route('dcmix/v1','/worker/configure',[
      'methods'=>'POST',
      'callback'=>static function(WP_REST_Request $request) {
          $digest=(string)$request->get_param('token_sha256');
          if (!preg_match('/^[a-f0-9]{64}$/D',$digest))
              return new WP_Error('dcmix_digest','Expected a SHA-256 worker verifier',['status'=>400]);
          update_option('dcmix_worker_token_hash',$digest,false);
          delete_option('dcmix_worker_token'); // obsolete plaintext key
          return ['configured'=>true];
      },
      'permission_callback'=>static function(){return current_user_can('manage_options');}
    ]);
    // Admin-only recovery for an already rendered and published MP3.
    // The worker's secret stays on the storage host; no token transfer.
    register_rest_route('dcmix/v1','/worker/reconcile',[
      'methods'=>'POST',
      'permission_callback'=>static function(){return current_user_can('manage_options');},
      'callback'=>static function(WP_REST_Request $request){
          try {return dcmix_report($request);}
          catch (Throwable $error) {
              return new WP_Error('dcmix_reconcile_exception',
                  get_class($error).': '.$error->getMessage().
                  ' ('.basename($error->getFile()).':'.$error->getLine().')',
                  ['status'=>500]);
          }
      }
    ]);
    register_rest_route('dcmix/v1','/worker/claim',[
      'methods'=>'POST','callback'=>'dcmix_claim','permission_callback'=>'dcmix_worker_auth'
    ]);
    register_rest_route('dcmix/v1','/worker/report',[
      'methods'=>'POST','callback'=>'dcmix_report','permission_callback'=>'dcmix_worker_auth'
    ]);
    register_rest_route('dcmix/v1','/worker/cleanup/list',[
      'methods'=>'POST','callback'=>'dcmix_cleanup_list','permission_callback'=>'dcmix_worker_auth'
    ]);
    register_rest_route('dcmix/v1','/worker/cleanup/ack',[
      'methods'=>'POST','callback'=>'dcmix_cleanup_ack','permission_callback'=>'dcmix_worker_auth'
    ]);
    register_rest_route('dcmix/v1','/worker/cleanup/queue',[
      'methods'=>'POST','callback'=>static function(WP_REST_Request $request){
         $post_id=absint($request->get_param('post_id'));
         if (!$post_id || get_post_status($post_id)!=='trash')
             return new WP_Error('dcmix_cleanup','Only a trashed generated mix can be queued',['status'=>422]);
         return ['queued'=>dcmix_mark_removed_mix($post_id)];
      },
      'permission_callback'=>static function(){return current_user_can('manage_options');}
    ]);
    register_rest_route('dcmix/v1','/jobs',[
      ['methods'=>'GET','callback'=>'dcmix_list_jobs','permission_callback'=>'dcmix_user_auth'],
      ['methods'=>'POST','callback'=>'dcmix_enqueue','permission_callback'=>'dcmix_user_auth']
    ]);
});

function dcmix_enqueue(WP_REST_Request $request) {
    global $wpdb;
    $user=get_current_user_id();
    $visibility=(string)($request->get_param('visibility')??'public');
    if (!in_array($visibility,['public','private'],true))
        return new WP_Error('dcmix_visibility','Choose public or private',['status'=>422]);
    if ($visibility==='public' && $request->get_param('rights_confirmed')!==true)
        return new WP_Error('dcmix_rights',
          'Confirm responsibility for publishing and redistribution rights before publishing.',
          ['status'=>422]);
    $list_id=absint($request->get_param('playlist_id'));
    if (!$list_id) return new WP_Error('dcmix_list','Choose a playlist',['status'=>400]);
    $source=dcmix_collect_tracks($list_id,$user);
    if (is_wp_error($source)) return $source;
    $bars=(int)($request->get_param('bars')??8);
    $advance=(int)($request->get_param('advance_bars')??4);
    if ($bars<1 || $bars>16 || $advance<0 || $advance>8)
        return new WP_Error('dcmix_settings','Invalid transition bars',['status'=>400]);
    $table=dcmix_table();
    $active=(int)$wpdb->get_var($wpdb->prepare(
      "SELECT COUNT(*) FROM $table WHERE user_id=%d AND status IN ('queued','running')",$user));
    if ($active>=1) return new WP_Error('dcmix_active','One active mix per member',['status'=>429]);
    $daily=(int)$wpdb->get_var($wpdb->prepare(
      "SELECT COUNT(*) FROM $table WHERE user_id=%d AND created_at >= %s",
      $user,gmdate('Y-m-d H:i:s',time()-86400)));
    if ($daily>=6) return new WP_Error('dcmix_daily','Six mix requests per 24 hours',['status'=>429]);
    $global=(int)$wpdb->get_var("SELECT COUNT(*) FROM $table WHERE status IN ('queued','running')");
    if ($global>=5) return new WP_Error('dcmix_busy','Renderer queue full',['status'=>429]);
    $id=wp_generate_uuid4();
    $job=['id'=>$id,'tracks'=>$source['tracks'],'bars'=>$bars,
          'advance_bars'=>$advance,'seed'=>42,
          'playlist_signature'=>dcmix_playlist_signature($list_id),
          'visibility'=>$visibility,
          'rights_confirmed_at'=>$visibility==='public'?gmdate('c'):null];
    $ok=$wpdb->insert($table,[
      'id'=>$id,'user_id'=>$user,'source_list_id'=>$list_id,
      'status'=>'queued','created_at'=>current_time('mysql',true),
      'request_json'=>wp_json_encode($job)
    ],['%s','%d','%d','%s','%s','%s']);
    if (!$ok) return new WP_Error('dcmix_db','Failed to queue mix',['status'=>500]);
    return new WP_REST_Response([
      'id'=>$id,'status'=>'queued','included'=>count($source['tracks']),
      'omitted'=>$source['omitted']
    ],201);
}

function dcmix_claim() {
    global $wpdb;
    $table=dcmix_table();
    // One worker is expected, but the conditional update also prevents
    // two workers from claiming the same pending row.
    for ($i=0;$i<4;$i++) {
        $row=$wpdb->get_row("SELECT id,request_json FROM $table WHERE status='queued' ORDER BY created_at ASC LIMIT 1");
        if (!$row) return ['job'=>null];
        $updated=$wpdb->query($wpdb->prepare(
          "UPDATE $table SET status='running',started_at=%s,attempts=attempts+1 WHERE id=%s AND status='queued'",
          current_time('mysql',true),(string)$row->id
        ));
        if ($updated===1) {
            $job=json_decode((string)$row->request_json,true);
            return ['job'=>$job];
        }
    }
    return ['job'=>null];
}

/**
 * Private files are NOT under the public media root. The ordinary WordPress login
 * cookie authorizes the first request; a ten-minute signed origin URL permits HTTP
 * Range requests without sending the raw MP3 through the WordPress PHP worker.
 * Signed URLs are bearer links and can be shared until expiry; users must keep them
 * private. No permanent/direct private origin URL is ever saved in post metadata.
 */
function dcmix_private_link(string $id,bool $download=false): string {
    return add_query_arg(['action'=>'dcmix_private_download','job'=>$id]
       +($download?['download'=>'1']:[]),admin_url('admin-post.php'));
}
function dcmix_signed_private_url(string $id,bool $download=false) {
    if (!wp_is_uuid($id))return false;
    $verifier=(string)get_option('dcmix_worker_token_hash','');
    if (!preg_match('/^[a-f0-9]{64}$/D',$verifier))return false;
    $key=hash('sha256',$verifier.'dcmix-private-url-v1');
    $path='/dc-media/private-mixes/'.$id.'.mp3';
    $expiry=time()+600;
    $sig=rtrim(strtr(base64_encode(md5($expiry.$path.' '.$key,true)),'+/','-_'),'=');
    return add_query_arg(['expires'=>$expiry,'sig'=>$sig]+($download?['download'=>'1']:[]),
                         home_url($path));
}
function dcmix_private_download() {
    if (!is_user_logged_in()) {
        if (!headers_sent())status_header(401);
        nocache_headers();
        exit('Please log in to download this private Mix Set.');
    }
    $id=(string)($_GET['job']??'');
    if (!wp_is_uuid($id)) {status_header(404);exit('Not found');}
    global $wpdb;
    $table=dcmix_table();
    $row=$wpdb->get_row($wpdb->prepare(
      "SELECT user_id,status,request_json,result_json FROM $table WHERE id=%s",$id));
    if (!$row || (int)$row->user_id!==get_current_user_id() || $row->status!=='completed') {
        status_header(404);exit('Not found');
    }
    $req=json_decode((string)$row->request_json,true);
    $result=json_decode((string)$row->result_json,true);
    if (($req['visibility']??'public')!=='private'
        || ($result['output_path']??'')!=='private-mixes/'.$id.'.mp3'
        || !preg_match('/^[a-f0-9]{64}$/',(string)($result['sha256']??''))) {
        status_header(404);exit('Not found');
    }
    $post_id=absint($result['post_id']??0);
    if (!$post_id || get_post_type($post_id)!=='mix'
        || get_post_status($post_id)!=='private'
        || (int)get_post_field('post_author',$post_id)!==get_current_user_id()
        || (string)get_post_meta($post_id,'_dcmix_job_id',true)!==$id) {
        status_header(404);exit('Not found');
    }
    $download=isset($_GET['download']) && $_GET['download']==='1';
    $url=dcmix_signed_private_url($id,$download);
    if (!$url) {status_header(503);exit('Private download service unavailable');}
    nocache_headers();
    header('X-Robots-Tag: noindex, nofollow',true);
    wp_safe_redirect($url,302,'Dancecore Private Mix');
    exit;
}
add_action('admin_post_dcmix_private_download','dcmix_private_download');
add_action('admin_post_nopriv_dcmix_private_download','dcmix_private_download');

/**
 * Make the generated remote MP3 behave like an ordinary native music attachment.
 * This filter applies ONLY to attachments minted by this isolated bridge.
 */
add_filter('wp_get_attachment_url',static function($url,$attachment_id){
    $job_id=(string)get_post_meta((int)$attachment_id,'_dcmix_job_id',true);
    if (!$job_id || !wp_is_uuid($job_id)) return $url;
    $file=(string)get_post_meta((int)$attachment_id,'_wp_attached_file',true);
    if ($file==='generated-mixes/'.$job_id.'.mp3')
        return home_url('/dc-media/'.$file);
    if ($file==='private-mixes/'.$job_id.'.mp3')
        return dcmix_private_link($job_id);
    return $url;
},120,2);

/**
 * Create a real WordPress Mix CPT attributed to the MEMBER WHO CLICKED
 * Generate, never to the playlist curator or the worker service.
 */
function dcmix_publish_mix($job,array $payload) {
    $job_id=(string)$job->id;
    $existing=get_posts([
      'post_type'=>'mix','post_status'=>['draft','pending','publish','private'],
      'posts_per_page'=>1,'fields'=>'ids','no_found_rows'=>true,
      'meta_key'=>'_dcmix_job_id','meta_value'=>$job_id
    ]);
    $mix_id=$existing?(int)$existing[0]:0;
    $user_id=(int)$job->user_id;
    $user=get_userdata($user_id);
    if (!$user || !post_type_exists('mix')) return new WP_Error(
       'dcmix_author','Missing original member or Mix post type',['status'=>503]);
    global $wpdb;
    $playlist=$wpdb->get_row($wpdb->prepare(
      "SELECT title FROM {$wpdb->prefix}dc_lists WHERE id=%d",(int)$job->source_list_id));
    $playlist_title=$playlist?sanitize_text_field((string)$playlist->title):'Community playlist';
    $title='Auto-DJ Mix — '.$playlist_title.' · '.gmdate('Y-m-d');
    $sources=json_decode((string)$job->request_json,true);
    $source_lines=[];
    foreach(($sources['tracks']??[]) as $index=>$track) {
        $track_id=absint($track['track_id']??0);
        if (!$track_id) continue;
        $artist=trim((string)get_post_meta($track_id,'artist',true));
        $track_title=get_the_title($track_id);
        $source_lines[]=($index+1).'. '.($artist!==''?$artist.' — ':'').$track_title;
    }
    $is_private=($sources['visibility']??'public')==='private';
    $description=$is_private
       ? 'Generated privately by the user via the auto-mix tool.'
       : 'Generated and published by the user via the auto-mix tool.';
    if (!$mix_id) {
        $created=wp_insert_post([
            'post_type'=>'mix','post_status'=>'draft','post_title'=>$title,
            // Stable, unique URL per generated job: never recycle the permalink
            // of a previously trashed mix from the same playlist/day.
            'post_name'=>sanitize_title('dj-mix-'.$playlist_title.'-'.substr($job_id,0,8)),
            'post_author'=>$user_id,'comment_status'=>'open',
            'post_content'=>$description
        ],true);
        if (is_wp_error($created)) return $created;
        $mix_id=(int)$created;
    }
    update_post_meta($mix_id,'_dcmix_job_id',$job_id);
    update_post_meta($mix_id,'title',$title);
    update_post_meta($mix_id,'artist',(string)$user->display_name);
    update_post_meta($mix_id,'info',$description);
    update_post_meta($mix_id,'license','CC0');
    update_post_meta($mix_id,'_dcx_relationship','ai');
    update_post_meta($mix_id,'_dcx_source_playlist',(int)$job->source_list_id);
    update_post_meta($mix_id,'_dcx_tracklist',implode("\n",$source_lines));
    update_post_meta($mix_id,'_dcmix_settings',[
        'bars'=>(int)($sources['bars']??8),
        'advance_bars'=>(int)($sources['advance_bars']??4),
        'source_track_ids'=>array_map('intval',array_column($sources['tracks']??[],'track_id')),
        'transitions'=>$payload['transitions']??[]
    ]);
    $attachment_id=absint(get_post_meta($mix_id,'file',true));
    $output=(string)$payload['output_path'];
    $url=$is_private?dcmix_private_link($job_id):home_url('/dc-media/'.$output);
    if (!$attachment_id) {
        // An interrupted earlier attempt can leave a parented attachment.
        // Reuse it rather than creating duplicate orphan entries.
        $existing_attachments=get_posts([
          'post_parent'=>$mix_id,'post_type'=>'attachment',
          'posts_per_page'=>1,'post_status'=>'inherit','fields'=>'ids'
        ]);
        if ($existing_attachments) $attachment_id=(int)$existing_attachments[0];
    }
    if (!$attachment_id) {
        // The file is already on Syburg, not on WordPress's filesystem.
        // The installed Creative Commons image plugin hooks add_attachment
        // and runs EXIF on all file types, including remote-only MP3s.
        // Match the site's archive importer: suppress ONLY for this insert,
        // then restore all callbacks even if the insertion throws.
        $saved_add_hooks=$GLOBALS['wp_filter']['add_attachment']??null;
        remove_all_actions('add_attachment');
        try {
            $created=wp_insert_attachment([
                'post_mime_type'=>'audio/mpeg','post_title'=>$title,
                'post_status'=>'inherit','post_parent'=>$mix_id,'guid'=>$url
            ],false,$mix_id,true,false);
        } finally {
            if ($saved_add_hooks) $GLOBALS['wp_filter']['add_attachment']=$saved_add_hooks;
        }
        if (is_wp_error($created)) return $created;
        $attachment_id=(int)$created;
    }
    update_post_meta($attachment_id,'_dcmix_job_id',$job_id);
    update_post_meta($attachment_id,'_wp_attached_file',$output);
    update_post_meta($attachment_id,'_dc_remote_path',$output);
    update_post_meta($attachment_id,'_dc_remote_sha256',(string)$payload['sha256']);
    update_post_meta($attachment_id,'_dc_remote_status','ready');
    update_post_meta($attachment_id,'_wp_attachment_metadata',[
        'mime_type'=>'audio/mpeg','filesize'=>(int)$payload['bytes']
    ]);
    update_post_meta($mix_id,'file',$attachment_id);
    if (wp_get_attachment_url($attachment_id)!==$url)
        return new WP_Error('dcmix_attachment','Generated audio URL could not be registered',['status'=>503]);
    $published=wp_update_post(['ID'=>$mix_id,'post_status'=>$is_private?'private':'publish'],true);
    if (is_wp_error($published)) return $published;
    return ['post_id'=>$mix_id,'post_url'=>get_permalink($mix_id)];
}

function dcmix_report(WP_REST_Request $request) {
    global $wpdb;
    $id=(string)$request->get_param('id');
    $status=(string)$request->get_param('status');
    if (!wp_is_uuid($id) || !in_array($status,['completed','failed'],true))
        return new WP_Error('dcmix_report','Invalid status or job id',['status'=>400]);
    $table=dcmix_table();
    $job=$wpdb->get_row($wpdb->prepare("SELECT * FROM $table WHERE id=%s",$id));
    if (!$job) return new WP_Error('dcmix_missing','Job not found',['status'=>404]);
    if ($job->status==='completed' || $job->status==='failed')
        return ['ok'=>true,'id'=>$id,'status'=>$job->status]; // idempotent retry
    if ($job->status!=='running')
        return new WP_Error('dcmix_state','Job was not claimed',['status'=>409]);
    $payload=['id'=>$id,'status'=>$status];
    $error='';
    if ($status==='completed') {
        $output=(string)$request->get_param('output_path');
        $sha=(string)$request->get_param('sha256');
        $bytes=(int)$request->get_param('bytes');
        $job_request=json_decode((string)$job->request_json,true);
        $private=($job_request['visibility']??'public')==='private';
        $expected=($private?'private-mixes/':'generated-mixes/').$id.'.mp3';
        if ($output!==$expected
            || !preg_match('/^[a-f0-9]{64}$/',$sha) || $bytes<1024 || $bytes>2147483648)
            return new WP_Error('dcmix_output','Invalid render receipt',['status'=>400]);
        $url=$private?dcmix_signed_private_url($id):home_url('/dc-media/'.$output);
        if (!$url)return new WP_Error('dcmix_private_config','Private URL signing unavailable',['status'=>503]);
        // A direct HEAD verifies the media origin actually serves the output.
        $head=wp_remote_head($url,['timeout'=>10,'redirection'=>0]);
        if (is_wp_error($head) || wp_remote_retrieve_response_code($head)!==200)
            return new WP_Error('dcmix_pending_output','Output has not reached the media origin',['status'=>503]);
        $seen=(int)wp_remote_retrieve_header($head,'content-length');
        if ($seen && $seen!==$bytes)
            return new WP_Error('dcmix_output_size','Output file length differs from receipt',['status'=>503]);
        $payload+=['output_path'=>$output,
          'url'=>$private?dcmix_private_link($id):$url,
          'sha256'=>$sha,'bytes'=>$bytes];
        $transitions=$request->get_param('transitions');
        if (is_array($transitions)) $payload['transitions']=array_slice($transitions,0,59);
        $published=dcmix_publish_mix($job,$payload);
        if (is_wp_error($published)) return $published;
        $payload+=$published;
    } else {
        $error=mb_substr(sanitize_text_field((string)$request->get_param('error')),0,250);
    }
    $updated=$wpdb->update($table,[
      'status'=>$status,'finished_at'=>current_time('mysql',true),
      'error_message'=>$error,'result_json'=>wp_json_encode($payload)
    ],['id'=>$id,'status'=>'running'],['%s','%s','%s','%s'],['%s','%s']);
    if ($updated!==1) return new WP_Error('dcmix_race','Job updated concurrently',['status'=>409]);
    // The user explicitly requested publication by clicking Generate mix.
    return ['ok'=>true,'id'=>$id,'status'=>$status,
      'download'=>$payload['url']??null,'mix_url'=>$payload['post_url']??null];
}

/**
 * A removed generated mix must also remove its UUID-named Syburg MP3.
 * No other CPTs, upload paths, or ordinary audio attachments are touched.
 * Deletion is asynchronous: Syburg fetches only previously authenticated
 * tombstones, verifies the stored SHA-256, unlinks the file, then ACKs.
 */
function dcmix_mark_removed_mix($post_id) {
    global $wpdb;
    $post_id=(int)$post_id;
    if (get_post_type($post_id)!=='mix') return false;
    $id=(string)get_post_meta($post_id,'_dcmix_job_id',true);
    if (!wp_is_uuid($id)) return false;
    $table=dcmix_table();
    $row=$wpdb->get_row($wpdb->prepare(
        "SELECT status,result_json FROM $table WHERE id=%s",$id));
    if (!$row || !in_array($row->status,['completed','delete_pending'],true))return false;
    $result=json_decode((string)$row->result_json,true);
    if (!is_array($result) || (int)($result['post_id']??0)!==$post_id
      || !in_array((string)($result['output_path']??''),
           ['generated-mixes/'.$id.'.mp3','private-mixes/'.$id.'.mp3'],true)
      || !preg_match('/^[a-f0-9]{64}$/',(string)($result['sha256']??'')))return false;
    if ($row->status==='delete_pending')return true;
    return $wpdb->update($table,['status'=>'delete_pending'],
       ['id'=>$id,'status'=>'completed'],['%s'],['%s','%s'])===1;
}
add_action('trashed_post','dcmix_mark_removed_mix',10,1);
add_action('before_delete_post','dcmix_mark_removed_mix',10,1);

/** Verify again that the mix is actually deleted/trashed before offering a purge. */
function dcmix_cleanup_list() {
    global $wpdb;
    $table=dcmix_table();
    $rows=$wpdb->get_results(
        "SELECT id,result_json FROM $table WHERE status='delete_pending' ORDER BY finished_at ASC LIMIT 15");
    $items=[];
    foreach($rows as $row) {
        $id=(string)$row->id;
        if (!wp_is_uuid($id))continue;
        $result=json_decode((string)$row->result_json,true);
        if (!is_array($result)
           || !in_array((string)($result['output_path']??''),
                ['generated-mixes/'.$id.'.mp3','private-mixes/'.$id.'.mp3'],true)
           || !preg_match('/^[a-f0-9]{64}$/',(string)($result['sha256']??'')))continue;
        $post_id=absint($result['post_id']??0);
        $status=$post_id?get_post_status($post_id):false;
        if ($status!==false && $status!=='trash')continue;
        $items[]=['id'=>$id,'sha256'=>(string)$result['sha256'],
          'storage'=>str_starts_with((string)$result['output_path'],'private-mixes/')?'private':'public'];
    }
    return ['items'=>$items];
}
function dcmix_cleanup_ack(WP_REST_Request $request) {
    global $wpdb;
    $id=(string)$request->get_param('id');
    if (!wp_is_uuid($id))return new WP_Error('dcmix_cleanup','Invalid ID',['status'=>400]);
    $table=dcmix_table();
    $row=$wpdb->get_row($wpdb->prepare(
      "SELECT status,result_json FROM $table WHERE id=%s",$id));
    if (!$row)return new WP_Error('dcmix_cleanup','Unknown job',['status'=>404]);
    if ($row->status==='deleted')return ['ok'=>true,'status'=>'deleted'];
    if ($row->status!=='delete_pending')
        return new WP_Error('dcmix_cleanup','Job not marked for deletion',['status'=>409]);
    $payload=json_decode((string)$row->result_json,true);
    $post_id=absint($payload['post_id']??0);
    $post_status=$post_id?get_post_status($post_id):false;
    if ($post_status!==false && $post_status!=='trash')
        return new WP_Error('dcmix_cleanup','Mix has been restored',['status'=>409]);
    $updated=$wpdb->update($table,['status'=>'deleted'],
        ['id'=>$id,'status'=>'delete_pending'],['%s'],['%s','%s']);
    if ($updated!==1)return new WP_Error('dcmix_cleanup','Concurrent update',['status'=>409]);
    $aid=$post_id?absint(get_post_meta($post_id,'file',true)):0;
    if ($aid && (string)get_post_meta($aid,'_dcmix_job_id',true)===$id) {
        // The public MP3 was already removed. Discard only its generated
        // attachment, not the source tracks or other uploaded media.
        wp_delete_attachment($aid,true);
        delete_post_meta($post_id,'file');
    }
    return ['ok'=>true,'status'=>'deleted'];
}

/** Restored trashed jobs must not become dead links. */
add_action('untrashed_post',static function($post_id){
    global $wpdb;
    if (get_post_type($post_id)!=='mix')return;
    $id=(string)get_post_meta($post_id,'_dcmix_job_id',true);
    if (!wp_is_uuid($id))return;
    $table=dcmix_table();
    $status=$wpdb->get_var($wpdb->prepare("SELECT status FROM $table WHERE id=%s",$id));
    if ($status==='delete_pending') {
        $wpdb->update($table,['status'=>'completed'],['id'=>$id,'status'=>'delete_pending'],
                       ['%s'],['%s','%s']);
    } elseif ($status==='deleted') {
        // The file is genuinely gone. Hold as draft until regenerated.
        wp_update_post(['ID'=>$post_id,'post_status'=>'draft']);
    }
},10,1);

function dcmix_list_jobs() {
    global $wpdb;
    $table=dcmix_table();
    $rows=$wpdb->get_results($wpdb->prepare(
      "SELECT id,source_list_id,status,created_at,finished_at,request_json,
              result_json,error_message
       FROM $table WHERE user_id=%d ORDER BY created_at DESC LIMIT 30",
      get_current_user_id()
    ),ARRAY_A);
    $signatures=[];
    foreach($rows as &$row) {
        $original=json_decode((string)$row['request_json'],true);
        $list_id=(int)$row['source_list_id'];
        if (!array_key_exists($list_id,$signatures))
            $signatures[$list_id]=dcmix_playlist_signature($list_id);
        $recorded=(string)($original['playlist_signature']??'');
        $row['playlist_changed']=$recorded!=='' && !hash_equals($recorded,$signatures[$list_id]);
        $row['result']=$row['result_json']?json_decode($row['result_json'],true):null;
        unset($row['result_json'],$row['request_json']);
        if ($row['result'] && isset($row['result']['post_id'])) {
            $post_status=get_post_status((int)$row['result']['post_id']);
            $expected_status=($original['visibility']??'public')==='private'?'private':'publish';
            if ($row['status']!=='completed' || $post_status!==$expected_status) {
                unset($row['result']['post_url'],$row['result']['url']);
                if ($row['status']==='completed')$row['status']='deleted';
            } else {
                // The post permalink may not have been refreshed when the
                // worker originally updated a draft to private/published.
                $row['result']['post_url']=get_permalink((int)$row['result']['post_id']);
            }
        }
    }
    return ['jobs'=>$rows];
}

/**
 * Only generated mixes get expandable source timestamps and DJ settings.
 * The original community single-mix template remains untouched.
 */
add_action('wp_footer',static function(){
    if (!is_singular('mix'))return;
    $post_id=get_queried_object_id();
    if (!$post_id || !wp_is_uuid((string)get_post_meta($post_id,'_dcmix_job_id',true)))return;
    $settings=get_post_meta($post_id,'_dcmix_settings',true);
    if (!is_array($settings))$settings=maybe_unserialize($settings);
    if (!is_array($settings))return;
    $ids=array_values(array_map('intval',$settings['source_track_ids']??[]));
    $transitions=is_array($settings['transitions']??null)?$settings['transitions']:[];
    $list_id=absint(get_post_meta($post_id,'_dcx_source_playlist',true));
    $url=$list_id?add_query_arg('dc_list',$list_id,home_url('/playlists/')):'';
    $html='<details class="dcmix-timeline"><summary>Track timestamps &amp; Auto-DJ settings</summary>';
    $html.='<div class="dcmix-timeline-body"><p>Crossfade: '.absint($settings['bars']??8).
      ' bars · starts '.absint($settings['advance_bars']??4).' bars earlier.';
    if ($url)$html.=' Source: <a href="'.esc_url($url).'">original playlist</a>.';
    $html.='</p><ol>';
    foreach($ids as $index=>$track_id){
        $position=$index===0?0.0:(float)($transitions[$index-1]['incoming_start_seconds']??0);
        $seconds=max(0,(int)round($position));
        $stamp=$seconds>=3600
          ?sprintf('%d:%02d:%02d',intdiv($seconds,3600),intdiv($seconds%3600,60),$seconds%60)
          :sprintf('%d:%02d',intdiv($seconds,60),$seconds%60);
        $name=$track_id?get_the_title($track_id):'';
        if (!$name)$name='Track '.($index+1).' (removed)';
        $artist=trim((string)get_post_meta($track_id,'artist',true));
        $html.='<li><time>'.esc_html($stamp).'</time> ';
        if ($track_id && get_post_status($track_id)==='publish')
            $html.='<a href="'.esc_url(get_permalink($track_id)).'">'.
                  esc_html(($artist!==''?$artist.' — ':'').$name).'</a>';
        else $html.=esc_html(($artist!==''?$artist.' — ':'').$name);
        $html.='</li>';
    }
    $html.='</ol></div></details>';
    ?>
    <template id="dcmix-track-timing-template"><?php echo $html; ?></template>
    <script>
    (function(){
        const template=document.getElementById('dcmix-track-timing-template');
        if(!template)return;
        const previous=document.querySelector('.dcx-single-tracklist');
        const target=previous||document.querySelector('.dcx-single-extra')||
                     document.querySelector('.dcx-single');
        if(!target)return;
        if(previous)previous.replaceWith(template.content.cloneNode(true));
        else target.append(template.content.cloneNode(true));
    })();
    </script>
    <?php
},16);

/** One-time synchronization for generated mixes created before the new labels. */
add_action('init',static function(){
    if (get_option('dcmix_auto_dj_labels_040'))return;
    $posts=get_posts(['post_type'=>'mix','post_status'=>'publish','posts_per_page'=>100,
      'fields'=>'ids','no_found_rows'=>true,'suppress_filters'=>true,
      'meta_key'=>'_dcmix_job_id','meta_compare'=>'EXISTS']);
    foreach($posts as $post_id){
        if (!wp_is_uuid((string)get_post_meta($post_id,'_dcmix_job_id',true)))continue;
        $title=get_the_title($post_id);
        if (!str_starts_with($title,'Auto-DJ Mix')){
            $list_id=absint(get_post_meta($post_id,'_dcx_source_playlist',true));
            global $wpdb;
            $name=$list_id?(string)$wpdb->get_var($wpdb->prepare(
                "SELECT title FROM {$wpdb->prefix}dc_lists WHERE id=%d",$list_id)):'Playlist';
            $title='Auto-DJ Mix — '.$name.' · '.get_post_time('Y-m-d',true,$post_id);
            wp_update_post(['ID'=>$post_id,'post_title'=>$title]);
        }
        $is_private=get_post_status($post_id)==='private';
    $description=$is_private
       ? 'Generated privately by the user via the auto-mix tool.'
       : 'Generated and published by the user via the auto-mix tool.';
        update_post_meta($post_id,'title',$title);
        update_post_meta($post_id,'info',$description);
        if (get_post_field('post_content',$post_id)!==$description)
            wp_update_post(['ID'=>$post_id,'post_content'=>$description]);
    }
    update_option('dcmix_auto_dj_labels_040','1',false);
},99);

add_action('wp_head',static function(){
    if (!is_singular('mix'))return;
    ?>
    <style>
    .dcmix-timeline{margin:9px 0 16px;border:1px solid #8b468b55;border-radius:3px;
      background:rgba(139,70,139,.035);padding:7px 10px;font-size:13px}
    .dcmix-timeline summary{cursor:pointer;font-weight:700}
    .dcmix-timeline-body{padding:5px 3px 2px}
    .dcmix-timeline-body p{margin:3px 0 7px}
    .dcmix-timeline ol{margin:3px 0 0;padding-left:20px}
    .dcmix-timeline li{margin:3px 0}
    .dcmix-timeline time{display:inline-block;min-width:3.5em;font-variant-numeric:tabular-nums}
    </style>
    <?php
});

add_shortcode('dcmix_generator',static function(){
    if (!is_user_logged_in())
        return '<p>Log in to generate and publish an Auto-DJ Mix Set.</p>';
    global $wpdb;
    $table=$wpdb->prefix.'dc_lists';
    $lists=$wpdb->get_results($wpdb->prepare(
      "SELECT id,title FROM $table WHERE user_id=%d OR visibility='public' ORDER BY id DESC LIMIT 100",
      get_current_user_id()
    ));
    $api=rest_url('dcmix/v1/jobs');
    $nonce=wp_create_nonce('wp_rest');
    ob_start();
    ?>
    <section class="dcmix-ui" style="max-width:690px">
      <p>Auto-DJ → MP3. Choose a playlist to generate a public or private Mix Set.
         Only eligible site-hosted CC0 tracks can be included.</p>
      <form id="dcmix-submit">
        <label>Playlist
          <select name="playlist_id" required>
          <?php foreach($lists as $list): ?>
            <option value="<?php echo absint($list->id); ?>"><?php echo esc_html($list->title); ?></option>
          <?php endforeach; ?>
          </select>
        </label>
        <label>Crossfade bars <input name="bars" type="number" min="1" max="16" value="8" style="width:65px"></label>
        <label>Earlier bars <input name="advance_bars" type="number" min="0" max="8" value="4" style="width:65px"></label>
        <span class="dcmix-tool-actions">
          <button type="submit" id="dcmix-tool-main">Generate Mix Set</button>
          <button type="button" id="dcmix-tool-recycle" hidden title="Regenerate Mix Set — playlist changed"
            aria-label="Regenerate Mix Set because playlist changed">↻</button>
        </span>
      </form>
      <p id="dcmix-message" role="status" aria-live="polite"></p>
    </section>
    <style>
    .dcmix-ui form{display:flex;align-items:end;gap:9px;flex-wrap:wrap;font-size:13px}
    .dcmix-ui form label{display:grid;gap:3px}
    .dcmix-ui form select{max-width:240px}
    .dcmix-ui #dcmix-tool-recycle{font-size:20px;line-height:1;border:1px solid #8b468b;
      padding:3px 7px;background:#f6f2f7;color:#53365a;border-radius:3px;margin-left:5px}
    .dcmix-ui #dcmix-tool-recycle[hidden]{display:none}
    </style>
    <script>
    (function(){
      const form=document.getElementById('dcmix-submit');
      if(!form)return;
      const main=document.getElementById('dcmix-tool-main'),
        recycle=document.getElementById('dcmix-tool-recycle'),
        msg=document.getElementById('dcmix-message'),
        select=form.querySelector('select[name="playlist_id"]');
      const api=<?php echo wp_json_encode($api); ?>,nonce=<?php echo wp_json_encode($nonce); ?>;
      let activeRequest=false;
      async function call(method,body){
        const response=await fetch(api,{method,credentials:'same-origin',cache:'no-store',
          headers:{'Content-Type':'application/json','X-WP-Nonce':nonce},
          body:body?JSON.stringify(body):undefined});
        const data=await response.json();
        if(!response.ok)throw Error(data.message||'Request failed');
        return data;
      }
      async function queue(){
        if(activeRequest)return;
        if(typeof window.dcmixConfirmRights!=='function')return;
        const visibility=await window.dcmixConfirmRights();
        if(!visibility)return;
        activeRequest=true;main.disabled=true;recycle.hidden=true;
        msg.textContent='Queueing…';
        const params=Object.fromEntries(new FormData(form));
        for(const key of ['playlist_id','bars','advance_bars'])params[key]=Number(params[key]);
        params.visibility=visibility;
        params.rights_confirmed=visibility==='public';
        try{
          const response=await call('POST',params);
          main.dataset.openUrl='';
          main.textContent='Generate Mix Set';
          msg.textContent='Queued '+response.included+' tracks ('+response.omitted.length+' excluded). Generating…';
          window.dispatchEvent(new CustomEvent('dcmix-job-queued',{detail:{id:response.id}}));
          refresh();
        }catch(error){main.disabled=false;msg.textContent=error.message;}
        finally{activeRequest=false;}
      }
      form.addEventListener('submit',event=>{
        event.preventDefault();
        if(main.dataset.openUrl){location.assign(main.dataset.openUrl);return;}
        queue();
      });
      recycle.addEventListener('click',()=>queue());
      select.addEventListener('change',refresh);
      async function refresh(){
        if(activeRequest)return;
        try{
          const data=await call('GET');
          const match=(data.jobs||[]).find(j=>Number(j.source_list_id)===Number(select.value));
          main.disabled=false;main.dataset.openUrl='';main.textContent='Generate Mix Set';
          recycle.hidden=true;msg.textContent='';
          if(!match)return;
          if(match.status==='queued'||match.status==='running'){
            main.disabled=true;
            msg.textContent=match.status==='queued'?'Queued…':'Generating Mix Set…';
          }else if(match.status==='completed'&&match.result&&match.result.post_url){
            main.textContent='Open Generated Mix Set';
            main.dataset.openUrl=match.result.post_url;
            recycle.hidden=match.playlist_changed!==true;
          }else if(match.status==='failed')msg.textContent='Last generation failed. You can retry.';
        }catch(error){msg.textContent=error.message;}
      }
      // The community guide still ships an obsolete 'preview / coming later'
      // announcement. Override its copy without touching the other plugin.
      function updateGuide(){
        const guide=document.querySelector('#dcx-community-guide');
        if(!guide)return;
        const title=guide.querySelector('.dcx-guide-heading');
        const message=guide.querySelector('.dcx-guide-message');
        const cta=guide.querySelector('.dcx-guide-cta');
        const obsolete=/AI Mix Generator|Create your first mix|Mixes & playlists|Curate a playlist/i.test(title?.textContent||'') ||
          /Auto-DJ.*(?:development|coming later)|no downloadable generated mix yet|AI audio rendering is in development/i.test(message?.textContent||'');
        if(!obsolete)return;
        const heading='Auto-DJ Mix Generator';
        const copy='Turn a playlist of eligible recordings into a downloadable MP3 Mix Set.';
        if(title&&title.textContent!==heading)title.textContent=heading;
        if(message&&message.textContent!==copy)message.textContent=copy;
        if(cta){
          if(cta.textContent!=='Generate Mix Set')cta.textContent='Generate Mix Set';
          if(cta.getAttribute('href')!=='/mix-generator/')cta.setAttribute('href','/mix-generator/');
        }
      }
      const guideCopy=document.querySelector('#dcx-community-guide');
      if(guideCopy)new MutationObserver(updateGuide).observe(guideCopy,{childList:true,subtree:true,characterData:true});
      updateGuide();
      document.addEventListener('dcx:navigated',()=>{updateGuide();place();});
      refresh();setInterval(refresh,12000);
    })();
    </script>
    <?php
    return ob_get_clean();
});

add_action('admin_menu',static function(){
    add_options_page('Dermixen worker','Dermixen worker','manage_options','dcmix-worker',static function(){
        if (!current_user_can('manage_options')) return;
        echo '<div class="wrap"><h1>Dermixen external worker</h1><p>The container authenticates outbound HTTPS requests. WordPress stores only a SHA-256 verifier, not the secret itself.</p>';
        echo '<p>Worker API: <code>'.esc_html(rest_url('dcmix/v1')).'</code></p>';
        echo '<p>Status: <strong>'.(get_option('dcmix_worker_token_hash')?'paired':'not paired').'</strong></p></div>';
    });
});


/** One accessible consent dialog shared by the directory and the tool page. */
add_action('wp_footer',static function(){
    if (!is_user_logged_in() || (!is_post_type_archive('mix') && !is_page('mix-generator')))return;
    ?>
    <style>
    #dcmix-rights-modal[hidden]{display:none!important}
    #dcmix-rights-modal{position:fixed;inset:0;z-index:20000;background:rgba(20,11,24,.65);
      display:grid;place-items:center;padding:12px}
    .dcmix-confirm-dialog{background:var(--global-palette9,#fff);color:var(--global-palette3,#292129);
      border:1px solid #91549c;border-radius:5px;box-shadow:0 8px 35px rgba(0,0,0,.28);
      width:min(440px,100%);padding:16px;font-size:13px;line-height:1.4}
    .dcmix-confirm-dialog h2{font-size:17px!important;line-height:1.2!important;margin:0 0 8px!important}
    .dcmix-confirm-dialog p{margin:0 0 10px}
    .dcmix-confirm-dialog label{display:flex;gap:8px;align-items:flex-start;cursor:pointer;margin:8px 0 13px}
    .dcmix-confirm-dialog input[type="checkbox"],.dcmix-confirm-dialog input[type="radio"]{margin:3px 0 0;flex:none}
    .dcmix-confirm-dialog fieldset{border:1px solid #b9a4bf;padding:5px 9px;margin:0 0 9px}
    .dcmix-confirm-dialog legend{font-weight:700;padding:0 4px}
    .dcmix-confirm-dialog fieldset label{margin:5px 0}
    .dcmix-confirm-dialog [hidden]{display:none!important}
    .dcmix-dialog-actions{display:flex;justify-content:flex-end;gap:8px}
    .dcmix-dialog-actions button{font:inherit;font-weight:700;font-size:13px;padding:6px 11px;
      border:1px solid #8b468b;border-radius:3px;cursor:pointer}
    .dcmix-dialog-cancel{background:transparent;color:inherit}
    .dcmix-dialog-accept{background:#8b468b;color:white}
    .dcmix-dialog-accept:disabled{opacity:.5;cursor:not-allowed}
    </style>
    <div id="dcmix-rights-modal" hidden>
      <div class="dcmix-confirm-dialog" role="dialog" aria-modal="true"
           aria-labelledby="dcmix-rights-title" tabindex="-1">
        <h2 id="dcmix-rights-title">Generate Mix Set</h2>
        <p>The Auto-DJ combines eligible playlist recordings into an MP3.
           Processing may take several minutes.</p>
        <fieldset class="dcmix-visibility"><legend>Who can access this Mix Set?</legend>
          <label><input type="radio" name="dcmix-visibility" value="public" checked>
            Publish publicly under my account</label>
          <label><input type="radio" name="dcmix-visibility" value="private">
            Keep private — only I can listen or download</label>
        </fieldset>
        <label id="dcmix-public-consent"><input id="dcmix-rights-check" type="checkbox">
          <span>I confirm the recordings are permitted for mixing and
          redistribution, and I accept responsibility for publishing this mix.</span>
        </label>
        <div class="dcmix-dialog-actions">
          <button type="button" class="dcmix-dialog-cancel">Cancel</button>
          <button type="button" class="dcmix-dialog-accept" disabled>Generate Mix Set</button>
        </div>
      </div>
    </div>
    <script>
    (function(){
      const backdrop=document.getElementById('dcmix-rights-modal'),
        dialog=backdrop.querySelector('[role="dialog"]'),
        check=document.getElementById('dcmix-rights-check'),
        consent=document.getElementById('dcmix-public-consent'),
        radios=[...backdrop.querySelectorAll('input[name="dcmix-visibility"]')],
        yes=backdrop.querySelector('.dcmix-dialog-accept'),
        no=backdrop.querySelector('.dcmix-dialog-cancel');
      let finish=null,priorFocus=null;
      function mode(){return radios.find(x=>x.checked)?.value||'public';}
      function sync(){
        consent.hidden=mode()==='private';
        yes.disabled=mode()==='public'&&!check.checked;
      }
      function close(ok){
        if(!finish)return;
        const resolve=finish;finish=null;
        backdrop.hidden=true;
        document.removeEventListener('keydown',keydown,true);
        if(priorFocus&&priorFocus.isConnected)priorFocus.focus();
        resolve(ok?mode():null);
      }
      function keydown(event){
        if(event.key==='Escape'){event.preventDefault();close(false);}
        if(event.key==='Tab'){
          const focusables=[...radios,...(consent.hidden?[]:[check]),no,yes].filter(x=>!x.disabled);
          const current=focusables.indexOf(document.activeElement);
          if(event.shiftKey && current===0){event.preventDefault();focusables[focusables.length-1].focus();}
          else if(!event.shiftKey && current===focusables.length-1){event.preventDefault();focusables[0].focus();}
        }
      }
      check.addEventListener('change',sync);
      radios.forEach(r=>r.addEventListener('change',sync));
      no.addEventListener('click',()=>close(false));
      yes.addEventListener('click',()=>{if(!yes.disabled)close(true);});
      backdrop.addEventListener('click',e=>{if(e.target===backdrop)close(false);});
      window.dcmixConfirmRights=function(){
        if(finish)return Promise.resolve(false);
        return new Promise(resolve=>{
          finish=resolve;priorFocus=document.activeElement;
          check.checked=false;radios[0].checked=true;sync();
          backdrop.hidden=false;document.addEventListener('keydown',keydown,true);
          dialog.focus();
        });
      };
    })();
    </script>
    <?php
},18);

/**
 * Inject small per-playlist Generate controls into the existing /mixes/
 * directory without modifying any Dancecore Community files or its votes.
 * The existing directory renderer owns the HTML rows; this bridge adds only
 * a compact, authenticated action after they have been rendered.
 */
add_action('wp_footer',static function(){
    if (!is_post_type_archive('mix'))return;
    $api=rest_url('dcmix/v1/jobs');
    $nonce=wp_create_nonce('wp_rest');
    $generator=home_url('/mix-generator/');
    $logged_in=is_user_logged_in();
    $login=wp_login_url(home_url('/mixes/'));
    ?>
    <style>
    .dcmix-quick{display:inline-flex;align-items:center;gap:5px;flex-wrap:wrap;margin:3px 7px 3px 0}
    .dcmix-quick button{font:inherit;font-size:12px;padding:4px 8px;border:1px solid #b9a4bf;
      background:#f6f2f7;color:#53365a;border-radius:3px;cursor:pointer;line-height:1.25}
    .dcmix-quick button:hover:not(:disabled){background:#eadfec}
    .dcmix-quick button:disabled{opacity:.65;cursor:default}
    .dcmix-quick .dcmix-recycle{font-size:18px;padding:0 5px;line-height:23px;font-weight:700}
    .dcmix-quick .dcmix-feedback{font-size:12px;color:#795580;display:inline-flex;gap:5px;align-items:center}
    .dcmix-quick .dcmix-feedback:empty{display:none}
    .dcmix-quick [hidden]{display:none!important}
    .dcmix-spinner{display:inline-block;width:13px;height:13px;vertical-align:-2px;flex:none;
      border:2px solid #d5b9d9;border-top-color:#713b81;border-radius:50%;
      animation:dcmix-rotate 850ms linear infinite}
    @keyframes dcmix-rotate{to{transform:rotate(360deg)}}
    @media(prefers-reduced-motion:reduce){.dcmix-spinner{animation:none}}
    </style>
    <script>
    (function(){
      const api=<?php echo wp_json_encode($api); ?>;
      const nonce=<?php echo wp_json_encode($nonce); ?>;
      const loggedIn=<?php echo wp_json_encode($logged_in); ?>;
      const loginUrl=<?php echo wp_json_encode($login); ?>;
      const generatorUrl=<?php echo wp_json_encode($generator); ?>;
      // Match the existing Browse artists card exactly, under it.
      const browse=document.querySelector('.dcx-side .dcxa-artists-cta');
      if(browse && !document.querySelector('.dcmix-generator-cta')){
        const box=document.createElement('a');
        box.className=browse.className+' dcmix-generator-cta';
        box.href=generatorUrl;
        const heading=document.createElement('strong');
        heading.textContent='Generate Mix Set from Playlist';
        const caption=document.createElement('span');
        caption.textContent='Auto-DJ → MP3 · choose a playlist';
        box.append(heading,caption);
        browse.insertAdjacentElement('afterend',box);
      }
      const results=document.querySelector('.dcx-playlist-results');
      if(!results)return;
      const entries=new Map();
      async function call(method,body){
        const response=await fetch(api,{method,credentials:'same-origin',cache:'no-store',
          headers:{'Content-Type':'application/json','X-WP-Nonce':nonce},
          body:body?JSON.stringify(body):undefined});
        const data=await response.json();
        if(!response.ok)throw Error(data.message||'Request failed');
        return data;
      }
      function clear(entry){
        entry.button.textContent='Generate Mix Set';
        entry.button.disabled=false;
        entry.button.dataset.openUrl='';
        entry.recycle.hidden=true;
        entry.feedback.replaceChildren();
      }
      async function submit(id,entry){
        if(!loggedIn){location.assign(loginUrl);return;}
        if(typeof window.dcmixConfirmRights!=='function')return;
        const visibility=await window.dcmixConfirmRights();
        if(!visibility)return;
        entry.button.disabled=true;
        entry.recycle.hidden=true;
        entry.feedback.textContent='Queueing…';
        try{
          const result=await call('POST',{
            playlist_id:id,bars:8,advance_bars:4,visibility,
            rights_confirmed:visibility==='public'
          });
          entry.button.dataset.openUrl='';
          entry.button.textContent='Generate Mix Set';
          entry.feedback.replaceChildren();
          const circle=document.createElement('span');
          circle.className='dcmix-spinner';
          circle.setAttribute('aria-hidden','true');
          entry.feedback.append(circle,document.createTextNode(' Queued'));
          window.dispatchEvent(new CustomEvent('dcmix-job-queued',{detail:{id:result.id}}));
          refresh();
        }catch(error){
          entry.feedback.textContent=error.message;
          entry.button.disabled=false;
        }
      }
      for(const article of results.querySelectorAll('.dcx-mix-list-result')){
        const link=article.querySelector('.dcx-mix-list-title');
        if(!link)continue;
        const id=Number(new URL(link.href,location.href).searchParams.get('dc_list'));
        if(!Number.isSafeInteger(id)||id<=0)continue;
        const holder=document.createElement('span');
        holder.className='dcmix-quick';
        const button=document.createElement('button');
        button.type='button';
        button.textContent='Generate Mix Set';
        button.title='Make a finished Auto-DJ MP3 from eligible tracks in this playlist';
        const recycle=document.createElement('button');
        recycle.type='button';
        recycle.className='dcmix-recycle';
        recycle.textContent='↻';
        recycle.title='Regenerate Mix Set — playlist changed since last generation';
        recycle.setAttribute('aria-label','Regenerate Mix Set because playlist changed');
        recycle.hidden=true;
        const feedback=document.createElement('span');
        feedback.className='dcmix-feedback';
        feedback.setAttribute('role','status');
        holder.append(button,recycle,feedback);
        const openPlaylist=article.querySelector('.dcx-list-open');
        if(openPlaylist)openPlaylist.before(holder);
        else article.append(holder);
        const entry={button,recycle,feedback};
        entries.set(id,entry);
        button.addEventListener('click',()=>{
          if(button.dataset.openUrl){location.assign(button.dataset.openUrl);return;}
          submit(id,entry);
        });
        recycle.addEventListener('click',()=>submit(id,entry));
      }
      async function refresh(){
        if(!loggedIn)return;
        try{
          const data=await call('GET');
          const seen=new Set();
          // Newest request for a playlist always wins over prior completed jobs.
          for(const job of data.jobs||[]){
            const id=Number(job.source_list_id);
            if(seen.has(id))continue;
            seen.add(id);
            const entry=entries.get(id);
            if(!entry)continue;
            clear(entry);
            if(job.status==='queued'||job.status==='running'){
              entry.button.disabled=true;
              const circle=document.createElement('span');
              circle.className='dcmix-spinner';
              circle.setAttribute('aria-hidden','true');
              entry.feedback.append(circle,document.createTextNode(job.status==='queued'?' Queued':' Generating…'));
            }else if(job.status==='completed'&&job.result&&job.result.post_url){
              entry.button.textContent='Open Generated Mix Set';
              entry.button.dataset.openUrl=job.result.post_url;
              entry.recycle.hidden=job.playlist_changed!==true;
            }else if(job.status==='failed'){
              entry.feedback.textContent='Generation failed';
            }
          }
        }catch(error){/* Leave previous status in place if offline. */}
      }
      refresh();
      setInterval(refresh,12000);
    })();
    </script>
    <?php
},20);

// This compact job indicator is visible on ANY logged-in page during a render,
// anchored below the existing help bubble where one is visible. The normal
// mix/player/archive DOM remains owned by the community and voting plugins.
add_action('wp_footer',static function(){
    if (!is_user_logged_in() || is_admin())return;
    $endpoint=rest_url('dcmix/v1/jobs');
    $nonce=wp_create_nonce('wp_rest');
    ?>
    <style>
    .dcmix-spinner {display:inline-block;width:13px;height:13px;vertical-align:-2px;flex:none;border:2px solid #d5b9d9;border-top-color:#713b81;border-radius:50%;animation:dcmix-rotate 850ms linear infinite}
    .dcmix-spinner[hidden]{display:none!important}
    @keyframes dcmix-rotate {to{transform:rotate(360deg)}}
    @media(prefers-reduced-motion:reduce){.dcmix-spinner{animation:none}}
    #dcmix-live-status[hidden]{display:none!important}
    #dcmix-live-status{position:absolute;z-index:9004;width:min(270px,calc(100vw - 20px));
      box-sizing:border-box;padding:9px 11px;background:#f8f3f9;
      border:1px solid #b995bd;border-radius:5px;box-shadow:0 3px 12px rgba(35,15,40,.18);
      color:#38263c;font-size:12px;line-height:1.35;text-align:left}
    #dcmix-live-status strong{font-size:13px}
    #dcmix-live-status .dcmix-status-line{display:flex;align-items:center;gap:8px}
    #dcmix-live-status .dcmix-status-detail{margin:4px 0 0 22px}
    #dcmix-live-status a{color:#713b81;font-weight:700;text-decoration:underline}
    #dcmix-live-status button{float:right;margin:-3px -4px 0 4px;border:0;background:transparent;
      color:#715d74;cursor:pointer;font-size:17px;line-height:1}
    </style>
    <script>
    (function(){
      const endpoint=<?php echo wp_json_encode($endpoint); ?>;
      const nonce=<?php echo wp_json_encode($nonce); ?>;
      const panel=document.createElement('aside');
      panel.id='dcmix-live-status';
      panel.setAttribute('role','status');
      panel.setAttribute('aria-live','polite');
      panel.setAttribute('aria-label','DJ mix generation');
      panel.hidden=true;
      panel.innerHTML='<button type="button" title="Dismiss" aria-label="Dismiss mix status">×</button>'+
        '<div class="dcmix-status-line"><span class="dcmix-spinner" aria-hidden="true"></span>'+
        '<strong>Generating mix</strong></div><div class="dcmix-status-detail"></div>';
      document.body.append(panel);
      const heading=panel.querySelector('strong');
      const ring=panel.querySelector('.dcmix-spinner');
      const details=panel.querySelector('.dcmix-status-detail');
      let lastJob='',dismissed='',requesting=false;

      function place(){
        const guide=document.querySelector('#dcx-community-guide');
        const rect=guide&&!guide.hidden?guide.getBoundingClientRect():null;
        // The status belongs to the LEFT help queue, including if a guide was dismissed.
        const left=rect&&rect.width>0?rect.left+window.scrollX:12+window.scrollX;
        const anchor=rect&&rect.height>0?rect.bottom+window.scrollY+8:
          (document.querySelector('.dcx-list-region')?.getBoundingClientRect().top||140)+window.scrollY;
        panel.style.left=Math.max(8,left)+'px';
        panel.style.right='auto';
        panel.style.top=Math.max(120,anchor)+'px';
        if(guide){
          const style=getComputedStyle(guide);
          for(const key of ['backgroundColor','color','borderColor','borderRadius','boxShadow','fontFamily'])
            if(style[key])panel.style[key]=style[key];
          if(rect&&rect.width>0)panel.style.width=Math.min(270,rect.width)+'px';
        }
      }
      function show(job){
        const active=['queued','running'].includes(job.status);
        const finished=job.status==='completed'&&job.result&&job.result.post_url;
        const failed=job.status==='failed';
        if(!active&&!finished&&!failed){panel.hidden=true;return;}
        if(dismissed===job.id && !active){panel.hidden=true;return;}
        lastJob=job.id;
        panel.hidden=false;
        ring.hidden=!active;
        heading.textContent=active?'Generating DJ mix':finished?'Mix ready':'Mix generation failed';
        details.replaceChildren();
        if(active){
          details.textContent=job.status==='queued'?'Waiting in queue…':'Mixing audio on storage server…';
        }else if(finished){
          const link=document.createElement('a');
          link.href=job.result.post_url;
          link.textContent='Open generated mix';
          details.append(link);
        }else{
          details.textContent=job.error_message||'Generation could not finish';
        }
        place();
      }
      async function refresh(){
        if(requesting)return;
        requesting=true;
        try{
          const r=await fetch(endpoint,{credentials:'same-origin',
            headers:{'X-WP-Nonce':nonce},cache:'no-store'});
          if(!r.ok)throw Error('Could not check generation');
          const data=await r.json();
          const jobs=Array.isArray(data.jobs)?data.jobs:[];
          const current=jobs.find(j=>j.status==='queued'||j.status==='running');
          // Show the newest finished/failed job for four hours, then disappear.
          const latest=jobs.find(j=>{
            if(!['completed','failed'].includes(j.status))return false;
            const when=Date.parse(j.created_at.replace(' ','T')+'Z');
            return Number.isFinite(when)&&Date.now()-when<4*3600*1000;
          });
          if(current)show(current);
          else if(latest)show(latest);
          else panel.hidden=true;
        }catch(error){/* Keep any existing indicator while briefly offline. */}
        finally{requesting=false;}
      }
      panel.querySelector('button').addEventListener('click',()=>{
        dismissed=lastJob;panel.hidden=true;
      });
      window.addEventListener('dcmix-job-queued',function(event){
        const id=event.detail&&event.detail.id;
        if(typeof id==='string'){dismissed='';show({id,status:'queued'});}
        refresh();
      });
      window.addEventListener('resize',place);
      const guide=document.querySelector('#dcx-community-guide');
      if(guide)new MutationObserver(place).observe(guide,{attributes:true,
        attributeFilter:['hidden','style','class']});
      // The community guide still ships an obsolete 'preview / coming later'
      // announcement. Override its copy without touching the other plugin.
      function updateGuide(){
        const guide=document.querySelector('#dcx-community-guide');
        if(!guide)return;
        const title=guide.querySelector('.dcx-guide-heading');
        const message=guide.querySelector('.dcx-guide-message');
        const cta=guide.querySelector('.dcx-guide-cta');
        const obsolete=/AI Mix Generator|Create your first mix|Mixes & playlists|Curate a playlist/i.test(title?.textContent||'') ||
          /Auto-DJ.*(?:development|coming later)|no downloadable generated mix yet|AI audio rendering is in development/i.test(message?.textContent||'');
        if(!obsolete)return;
        const heading='Auto-DJ Mix Generator';
        const copy='Turn a playlist of eligible recordings into a downloadable MP3 Mix Set.';
        if(title&&title.textContent!==heading)title.textContent=heading;
        if(message&&message.textContent!==copy)message.textContent=copy;
        if(cta){
          if(cta.textContent!=='Generate Mix Set')cta.textContent='Generate Mix Set';
          if(cta.getAttribute('href')!=='/mix-generator/')cta.setAttribute('href','/mix-generator/');
        }
      }
      const guideCopy=document.querySelector('#dcx-community-guide');
      if(guideCopy)new MutationObserver(updateGuide).observe(guideCopy,{childList:true,subtree:true,characterData:true});
      updateGuide();
      document.addEventListener('dcx:navigated',()=>{updateGuide();place();});
      refresh();setInterval(refresh,12000);
    })();
    </script>
    <?php
},22);

/**
 * Guests also need truthful generator guidance. The legacy layout plugin still
 * says rendering is in development; do not edit that plugin while other work
 * is ongoing. This read-only UI override is scoped to the outdated help card.
 */
add_action('wp_footer',static function(){
    if (is_user_logged_in() || is_admin()) return;
    $login=wp_login_url(home_url('/mix-generator/'));
    ?>
    <script>
    (function(){
      const login=<?php echo wp_json_encode($login); ?>;
      function update(){
        const guide=document.getElementById('dcx-community-guide');
        if(!guide)return;
        const heading=guide.querySelector('.dcx-guide-heading');
        const message=guide.querySelector('.dcx-guide-message');
        const cta=guide.querySelector('.dcx-guide-cta');
        const stale=/AI Mix Generator|Create your first mix|Mixes & playlists|Curate a playlist/i.test(heading?.textContent||'')||
          /(?:AI audio rendering is in development|no downloadable generated mix yet|AI Auto-DJ is in development|coming later)/i.test(message?.textContent||'');
        if(!stale)return;
        if(heading)heading.textContent='Auto-DJ Mix Generator';
        if(message)message.textContent='Sign in to generate a downloadable MP3 Mix Set from eligible playlist tracks.';
        if(cta){cta.textContent='Sign in to generate a mix';cta.href=login;}
      }
      const guide=document.getElementById('dcx-community-guide');
      if(guide)new MutationObserver(update).observe(guide,{childList:true,subtree:true,characterData:true});
      update();document.addEventListener('dcx:navigated',update);
    })();
    </script>
    <?php
},23);

/** Only the private download button requests attachment disposition.
 *  The HTML5 audio source continues to request inline, range-enabled media.
 */
add_action('wp_footer',static function(){
    if (!is_user_logged_in() || is_admin())return;
    ?>
    <script>
    (function(){
      function fixPrivateDownload(){
        for(const link of document.querySelectorAll('a.dcx-native-download[href]')){
          let url;try{url=new URL(link.href,location.href);}catch(_){continue;}
          if(url.origin!==location.origin ||
             url.searchParams.get('action')!=='dcmix_private_download')continue;
          if(url.searchParams.get('download')!=='1'){
            url.searchParams.set('download','1');
            link.href=url.href;
          }
          const title=document.querySelector('.entry-title')?.textContent?.trim()||'Auto-DJ Mix';
          link.download=title.replace(/[\\/:*?"<>|]/g,'').slice(0,100)+'.mp3';
        }
      }
      fixPrivateDownload();document.addEventListener('dcx:navigated',fixPrivateDownload);
    })();
    </script>
    <?php
},24);