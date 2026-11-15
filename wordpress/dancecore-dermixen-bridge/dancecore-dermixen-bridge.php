<?php
/**
 * Plugin Name: Dancecore Dermixen Bridge
 * Description: Site-side job queue for the external Dermixen container. Audio is never mixed in PHP.
 * Version: 0.1.0
 * Requires PHP: 8.0
 */
defined('ABSPATH') || exit;
const DCMIX_BRIDGE_VERSION = '0.1.0';

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
    if (!get_option('dcmix_worker_token')) {
        add_option('dcmix_worker_token',bin2hex(random_bytes(32)),'','no');
    }
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
    $expected = (string)get_option('dcmix_worker_token','');
    return strlen($expected)===64 && hash_equals($expected,$matches[1])
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
    register_rest_route('dcmix/v1','/worker/claim',[
      'methods'=>'POST','callback'=>'dcmix_claim','permission_callback'=>'dcmix_worker_auth'
    ]);
    register_rest_route('dcmix/v1','/worker/report',[
      'methods'=>'POST','callback'=>'dcmix_report','permission_callback'=>'dcmix_worker_auth'
    ]);
    register_rest_route('dcmix/v1','/jobs',[
      ['methods'=>'GET','callback'=>'dcmix_list_jobs','permission_callback'=>'dcmix_user_auth'],
      ['methods'=>'POST','callback'=>'dcmix_enqueue','permission_callback'=>'dcmix_user_auth']
    ]);
});

function dcmix_enqueue(WP_REST_Request $request) {
    global $wpdb;
    $user=get_current_user_id();
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
    if ($daily>=2) return new WP_Error('dcmix_daily','Two mix requests per 24 hours',['status'=>429]);
    $global=(int)$wpdb->get_var("SELECT COUNT(*) FROM $table WHERE status IN ('queued','running')");
    if ($global>=5) return new WP_Error('dcmix_busy','Renderer queue full',['status'=>429]);
    $id=wp_generate_uuid4();
    $job=['id'=>$id,'tracks'=>$source['tracks'],'bars'=>$bars,
          'advance_bars'=>$advance,'seed'=>42];
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
        if ($output!=='generated-mixes/'.$id.'.mp3'
            || !preg_match('/^[a-f0-9]{64}$/',$sha) || $bytes<1024 || $bytes>2147483648)
            return new WP_Error('dcmix_output','Invalid render receipt',['status'=>400]);
        $url=home_url('/dc-media/'.$output);
        // A direct HEAD verifies the media origin actually serves the output.
        $head=wp_remote_head($url,['timeout'=>10,'redirection'=>0]);
        if (is_wp_error($head) || wp_remote_retrieve_response_code($head)!==200)
            return new WP_Error('dcmix_pending_output','Output has not reached the media origin',['status'=>503]);
        $seen=(int)wp_remote_retrieve_header($head,'content-length');
        if ($seen && $seen!==$bytes)
            return new WP_Error('dcmix_output_size','Output file length differs from receipt',['status'=>503]);
        $payload+=['output_path'=>$output,'url'=>$url,'sha256'=>$sha,'bytes'=>$bytes];
        $transitions=$request->get_param('transitions');
        if (is_array($transitions)) $payload['transitions']=array_slice($transitions,0,59);
    } else {
        $error=mb_substr(sanitize_text_field((string)$request->get_param('error')),0,250);
    }
    $updated=$wpdb->update($table,[
      'status'=>$status,'finished_at'=>current_time('mysql',true),
      'error_message'=>$error,'result_json'=>wp_json_encode($payload)
    ],['id'=>$id,'status'=>'running'],['%s','%s','%s','%s'],['%s','%s']);
    if ($updated!==1) return new WP_Error('dcmix_race','Job updated concurrently',['status'=>409]);
    // Never auto-publish a generated mix. The member controls publication.
    return ['ok'=>true,'id'=>$id,'status'=>$status,'download'=>$payload['url']??null];
}

function dcmix_list_jobs() {
    global $wpdb;
    $table=dcmix_table();
    $rows=$wpdb->get_results($wpdb->prepare(
      "SELECT id,source_list_id,status,created_at,finished_at,result_json,error_message
       FROM $table WHERE user_id=%d ORDER BY created_at DESC LIMIT 30",
      get_current_user_id()
    ),ARRAY_A);
    foreach($rows as &$row) {
        $row['result']=$row['result_json']?json_decode($row['result_json'],true):null;
        unset($row['result_json']);
    }
    return ['jobs'=>$rows];
}

add_shortcode('dcmix_generator',static function(){
    if (!is_user_logged_in()) return '<p>Log in to create a DJ mix.</p>';
    global $wpdb;
    $table=$wpdb->prefix.'dc_lists';
    $lists=$wpdb->get_results($wpdb->prepare(
      "SELECT id,title FROM $table WHERE user_id=%d OR visibility='public' ORDER BY id DESC LIMIT 100",
      get_current_user_id()
    ));
    $api=esc_url_raw(rest_url('dcmix/v1/jobs'));
    $nonce=wp_create_nonce('wp_rest');
    ob_start();
    echo '<div class="dcmix-ui" style="max-width:680px"><p>Create a continuous DJ mix from eligible CC0 on-site tracks. External recordings are excluded.</p>';
    echo '<form id="dcmix-submit"><label>Playlist <select name="playlist_id" required>';
    foreach($lists as $list)
        echo '<option value="'.absint($list->id).'">'.esc_html($list->title).'</option>';
    echo '</select></label> <label>Fade bars <input name="bars" type="number" min="1" max="16" value="8" style="width:60px"></label> ';
    echo '<label>Earlier bars <input name="advance_bars" type="number" min="0" max="8" value="4" style="width:60px"></label> ';
    echo '<button type="submit">Generate mix</button></form><p id="dcmix-message" role="status"></p><div id="dcmix-jobs"></div></div>';
    echo '<script>(function(){const root=document.querySelector(".dcmix-ui"),form=document.getElementById("dcmix-submit"),message=document.getElementById("dcmix-message"),jobs=document.getElementById("dcmix-jobs");';
    echo 'const api='.wp_json_encode($api).',nonce='.wp_json_encode($nonce).';';
    echo 'async function call(method,body){const res=await fetch(api,{method,credentials:"same-origin",headers:{"Content-Type":"application/json","X-WP-Nonce":nonce},body:body?JSON.stringify(body):undefined});const data=await res.json();if(!res.ok)throw Error(data.message||"Request failed");return data;}';
    echo 'async function refresh(){try{const data=await call("GET");jobs.replaceChildren();for(const j of data.jobs){const p=document.createElement("p");p.textContent=j.created_at+" · "+j.status+" · "+j.id.slice(0,8);if(j.result&&j.result.url){const a=document.createElement("a");a.href=j.result.url;a.textContent=" · Download MP3";p.append(a);}if(j.error_message)p.append(" · "+j.error_message);jobs.append(p);}}catch(e){message.textContent=e.message;}}';
    echo 'form.addEventListener("submit",async e=>{e.preventDefault();const data=Object.fromEntries(new FormData(form));for(const key of ["playlist_id","bars","advance_bars"])data[key]=Number(data[key]);message.textContent="Submitting...";try{const r=await call("POST",data);message.textContent="Queued "+r.included+" tracks ("+r.omitted.length+" ineligible skipped).";refresh();}catch(err){message.textContent=err.message;}});refresh();setInterval(refresh,30000);})();</script>';
    return ob_get_clean();
});

add_action('admin_menu',static function(){
    add_options_page('Dermixen worker','Dermixen worker','manage_options','dcmix-worker',static function(){
        if (!current_user_can('manage_options')) return;
        if (isset($_POST['dcmix_rotate'])) {
            check_admin_referer('dcmix_rotate');
            update_option('dcmix_worker_token',bin2hex(random_bytes(32)),false);
            echo '<div class="notice notice-success"><p>Worker token rotated. Update worker.env before restarting the container.</p></div>';
        }
        echo '<div class="wrap"><h1>Dermixen external worker</h1><p>This key is private. The Docker worker uses it for outbound HTTPS polling; WordPress never runs FFmpeg.</p>';
        echo '<p>DCMIX_API: <code>'.esc_html(rest_url('dcmix/v1')).'</code></p>';
        echo '<p>DCMIX_WORKER_TOKEN: <code>'.esc_html((string)get_option('dcmix_worker_token')).'</code></p>';
        echo '<form method="post">';
        wp_nonce_field('dcmix_rotate');
        echo '<button class="button" type="submit" name="dcmix_rotate" value="1">Rotate token</button></form></div>';
    });
});
