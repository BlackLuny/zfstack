0000000000272620 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump>:
  272620:	push   %rbp
  272621:	push   %r15
  272623:	push   %r14
  272625:	push   %r13
  272627:	push   %r12
  272629:	push   %rbx
  27262a:	sub    $0x648,%rsp
  272631:	mov    %rcx,0x48(%rsp)
  272636:	mov    %rdx,0x8(%rsp)
  27263b:	cmpq   $0x0,0x9e8(%rdi)
  272643:	je     272d68 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x748>
  272649:	mov    %rsi,%rbx
  27264c:	mov    %rdi,%r14
  27264f:	mov    0x9f0(%rdi),%rdi
  272656:	mov    0x9f8(%r14),%rsi
  27265d:	mov    %rbx,%rdx
  272660:	call   13aa80 <core::hash::BuildHasher::hash_one>
  272665:	mov    0x9d0(%r14),%rcx
  27266c:	mov    0x9d8(%r14),%rdx
  272673:	mov    %rdx,%rsi
  272676:	and    %rax,%rsi
  272679:	shr    $0x39,%rax
  27267d:	movd   %eax,%xmm0
  272681:	punpcklbw %xmm0,%xmm0
  272685:	pshuflw $0x0,%xmm0,%xmm0
  27268a:	pshufd $0x0,%xmm0,%xmm0
  27268f:	lea    -0x78(%rcx),%rax
  272693:	xor    %edi,%edi
  272695:	pcmpeqd %xmm1,%xmm1
  272699:	movdqu (%rcx,%rsi,1),%xmm2
  27269e:	movdqa %xmm2,%xmm3
  2726a2:	pcmpeqb %xmm0,%xmm3
  2726a6:	pmovmskb %xmm3,%r8d
  2726ab:	test   %r8d,%r8d
  2726ae:	je     2726d5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb5>
  2726b0:	tzcnt  %r8d,%r9d
  2726b5:	add    %rsi,%r9
  2726b8:	and    %rdx,%r9
  2726bb:	neg    %r9
  2726be:	imul   $0x78,%r9,%r9
  2726c2:	cmp    %rbx,(%rax,%r9,1)
  2726c6:	je     2726f7 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd7>
  2726c8:	lea    -0x1(%r8),%r9d
  2726cc:	and    %r8w,%r9w
  2726d0:	mov    %r9d,%r8d
  2726d3:	jne    2726b0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x90>
  2726d5:	pcmpeqb %xmm1,%xmm2
  2726d9:	pmovmskb %xmm2,%r8d
  2726de:	test   %r8d,%r8d
  2726e1:	jne    272d68 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x748>
  2726e7:	add    %rdi,%rsi
  2726ea:	add    $0x10,%rsi
  2726ee:	add    $0x10,%rdi
  2726f2:	and    %rdx,%rsi
  2726f5:	jmp    272699 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x79>
  2726f7:	mov    %r14,%rax
  2726fa:	lea    (%rcx,%r9,1),%r14
  2726fe:	lea    (%rcx,%r9,1),%rdx
  272702:	add    $0xffffffffffffff90,%rdx
  272706:	mov    %rdx,0xa8(%rsp)
  27270e:	mov    0x8(%rsp),%rdx
  272713:	lea    0x190(%rdx),%rdx
  27271a:	mov    %rdx,0xa0(%rsp)
  272722:	movq   $0x0,0x18(%rsp)
  27272b:	lea    0x348(%rax),%rdx
  272732:	mov    %rdx,0x88(%rsp)
  27273a:	mov    %rax,0x10(%rsp)
  27273f:	add    $0x6f8,%rax
  272745:	mov    %rax,0x20(%rsp)
  27274a:	lea    -0x40(%rcx,%r9,1),%rax
  27274f:	mov    %rax,(%rsp)
  272753:	mov    %ebx,%eax
  272755:	mov    %rbx,0x50(%rsp)
  27275a:	shr    $0x20,%rbx
  27275e:	mov    %rbx,0x58(%rsp)
  272763:	lea    0x238(%rsp),%rbx
  27276b:	mov    %rax,0x60(%rsp)
  272770:	imul   $0xb8,%rax,%rax
  272777:	mov    %rax,0x28(%rsp)
  27277c:	nopl   0x0(%rax)
  272780:	mov    -0x20(%r14),%rax
  272784:	cmp    $0x7ffff,%rax
  27278a:	ja     272920 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x300>
  272790:	mov    %r12,%rcx
  272793:	data16 data16 data16 cs nopw 0x0(%rax,%rax,1)
  2727a0:	cmpb   $0x1,-0x8(%r14)
  2727a5:	je     272917 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2f7>
  2727ab:	mov    $0x303,%r12d
  2727b1:	mov    0x10(%rsp),%rdx
  2727b6:	mov    0x60(%rsp),%rsi
  2727bb:	cmp    0x4a0(%rdx),%rsi
  2727c2:	jae    2728e0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2c0>
  2727c8:	mov    0x498(%rdx),%rdx
  2727cf:	mov    0x58(%rsp),%rsi
  2727d4:	mov    0x28(%rsp),%rdi
  2727d9:	cmp    %esi,0xb0(%rdx,%rdi,1)
  2727e0:	jne    2728e0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2c0>
  2727e6:	add    %rdi,%rdx
  2727e9:	cmpw   $0x2,(%rdx)
  2727ed:	jne    2728d1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2b1>
  2727f3:	mov    0xa8(%rdx),%rsi
  2727fa:	test   %rsi,%rsi
  2727fd:	je     2728e0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2c0>
  272803:	mov    $0x80000,%edx
  272808:	sub    %rax,%rdx
  27280b:	lea    0x230(%rsp),%rdi
  272813:	mov    0x88(%rsp),%rcx
  27281b:	mov    0x20(%rsp),%r8
  272820:	mov    0x48(%rsp),%r9
  272825:	call   1ba7c0 <zfstack::conn::Conn::read_chunk_for_adapter>
  27282a:	mov    0x10(%rsp),%rdi
  27282f:	mov    0x50(%rsp),%rsi
  272834:	call   1cc0e0 <zfstack::shard::Shard::after_conn_change>
  272839:	mov    0x230(%rsp),%r15
  272841:	mov    0x238(%rsp),%r12
  272849:	test   %r15,%r15
  27284c:	je     2728e0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2c0>
  272852:	mov    0x240(%rsp),%rbp
  27285a:	mov    0x248(%rsp),%r13
  272862:	add    %rbp,-0x20(%r14)
  272866:	mov    -0x40(%r14),%rcx
  27286a:	mov    -0x28(%r14),%rax
  27286e:	cmp    %rcx,%rax
  272871:	jne    272884 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x264>
  272873:	mov    (%rsp),%rdi
  272877:	call   1498d0 <alloc::collections::vec_deque::VecDeque<T,A>::grow>
  27287c:	mov    -0x40(%r14),%rcx
  272880:	mov    -0x28(%r14),%rax
  272884:	mov    -0x30(%r14),%rdx
  272888:	add    %rax,%rdx
  27288b:	cmp    %rcx,%rdx
  27288e:	mov    $0x0,%esi
  272893:	cmovb  %rsi,%rcx
  272897:	sub    %rcx,%rdx
  27289a:	mov    -0x38(%r14),%rcx
  27289e:	shl    $0x5,%rdx
  2728a2:	mov    %r15,(%rcx,%rdx,1)
  2728a6:	mov    %r12,0x8(%rcx,%rdx,1)
  2728ab:	mov    %rbp,0x10(%rcx,%rdx,1)
  2728b0:	mov    %r13,0x18(%rcx,%rdx,1)
  2728b5:	inc    %rax
  2728b8:	mov    %rax,-0x28(%r14)
  2728bc:	mov    -0x20(%r14),%rax
  2728c0:	mov    %r12,%rcx
  2728c3:	cmp    $0x80000,%rax
  2728c9:	jb     2727a0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x180>
  2728cf:	jmp    272920 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x300>
  2728d1:	and    $0xffffffffffffff00,%rcx
  2728d8:	or     $0x2,%rcx
  2728dc:	mov    %rcx,%r12
  2728df:	nop
  2728e0:	cmp    $0x2,%r12b
  2728e4:	je     2728fd <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2dd>
  2728e6:	movzbl %r12b,%eax
  2728ea:	cmp    $0x3,%eax
  2728ed:	jne    272920 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x300>
  2728ef:	mov    %r12d,%eax
  2728f2:	shr    $0x8,%eax
  2728f5:	test   %al,%al
  2728f7:	jne    272d6f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x74f>
  2728fd:	movb   $0x1,-0x8(%r14)
  272902:	mov    -0x20(%r14),%rax
  272906:	mov    %r12,%rcx
  272909:	cmp    $0x80000,%rax
  27290f:	jb     2727a0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x180>
  272915:	jmp    272920 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x300>
  272917:	mov    %rcx,%r12
  27291a:	nopw   0x0(%rax,%rax,1)
  272920:	cmpq   $0x0,-0x28(%r14)
  272925:	je     273108 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xae8>
  27292b:	mov    $0x38,%eax
  272930:	movq   $0x1,0x1f8(%rsp,%rax,1)
  27293c:	movq   $0x0,0x200(%rsp,%rax,1)
  272948:	movq   $0x1,0x208(%rsp,%rax,1)
  272954:	movq   $0x0,0x210(%rsp,%rax,1)
  272960:	movq   $0x1,0x218(%rsp,%rax,1)
  27296c:	movq   $0x0,0x220(%rsp,%rax,1)
  272978:	movq   $0x1,0x228(%rsp,%rax,1)
  272984:	movq   $0x0,0x230(%rsp,%rax,1)
  272990:	add    $0x40,%rax
  272994:	cmp    $0x438,%rax
  27299a:	jne    272930 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x310>
  27299c:	mov    -0x28(%r14),%rdx
  2729a0:	test   %rdx,%rdx
  2729a3:	je     2729e0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x3c0>
  2729a5:	mov    -0x40(%r14),%rcx
  2729a9:	mov    -0x30(%r14),%rax
  2729ad:	cmp    %rcx,%rax
  2729b0:	mov    $0x0,%esi
  2729b5:	cmovae %rcx,%rsi
  2729b9:	sub    %rsi,%rax
  2729bc:	mov    %rcx,%rdi
  2729bf:	sub    %rax,%rdi
  2729c2:	mov    %rdx,%rsi
  2729c5:	sub    %rdi,%rsi
  2729c8:	ja     2729e6 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x3c6>
  2729ca:	add    %rax,%rdx
  2729cd:	mov    %rdx,%rcx
  2729d0:	jmp    2729e4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x3c4>
  2729d2:	data16 data16 data16 data16 cs nopw 0x0(%rax,%rax,1)
  2729e0:	xor    %eax,%eax
  2729e2:	xor    %ecx,%ecx
  2729e4:	xor    %esi,%esi
  2729e6:	mov    -0x38(%r14),%rdi
  2729ea:	shl    $0x5,%rax
  2729ee:	add    %rdi,%rax
  2729f1:	shl    $0x5,%rcx
  2729f5:	add    %rdi,%rcx
  2729f8:	shl    $0x5,%rsi
  2729fc:	add    %rdi,%rsi
  2729ff:	mov    %rbx,%r8
  272a02:	xor    %edx,%edx
  272a04:	jmp    272a38 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x418>
  272a06:	cs nopw 0x0(%rax,%rax,1)
  272a10:	mov    %rcx,%r9
  272a13:	mov    %rsi,%rcx
  272a16:	mov    %rcx,%rsi
  272a19:	movdqu 0x8(%rax),%xmm0
  272a1e:	add    $0x20,%rax
  272a22:	movdqu %xmm0,-0x8(%r8)
  272a28:	inc    %rdx
  272a2b:	add    $0x10,%r8
  272a2f:	mov    %r9,%rcx
  272a32:	cmp    $0x40,%rdx
  272a36:	je     272a60 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x440>
  272a38:	cmp    %rcx,%rax
  272a3b:	jne    272a10 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x3f0>
  272a3d:	test   %rdi,%rdi
  272a40:	je     272a50 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x430>
  272a42:	mov    %rdi,%rax
  272a45:	mov    %rsi,%r9
  272a48:	cmp    %rsi,%rdi
  272a4b:	mov    %rcx,%rdi
  272a4e:	jne    272a16 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x3f6>
  272a50:	cmp    $0x41,%rdx
  272a54:	jb     272a65 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x445>
  272a56:	jmp    274861 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2241>
  272a5b:	nopl   0x0(%rax,%rax,1)
  272a60:	mov    $0x40,%edx
  272a65:	mov    0xa8(%rsp),%rdi
  272a6d:	lea    0x230(%rsp),%rsi
  272a75:	call   3c0a20 <tokio::net::tcp::stream::TcpStream::try_write_vectored>
  272a7a:	mov    %rdx,%rbp
  272a7d:	test   %rax,%rax
  272a80:	je     272af0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x4d0>
  272a82:	mov    %rbp,%rdi
  272a85:	call   459e0 <std::io::error::Error::kind>
  272a8a:	cmp    $0xd,%al
  272a8c:	jne    272c30 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x610>
  272a92:	mov    %rbp,0x150(%rsp)
  272a9a:	mov    -0x60(%r14),%rsi
  272a9e:	lea    0xb0(%rsp),%rdi
  272aa6:	mov    0xa0(%rsp),%rdx
  272aae:	mov    $0x1,%ecx
  272ab3:	call   3bf530 <tokio::runtime::io::registration::Registration::poll_ready>
  272ab8:	movzbl 0xb9(%rsp),%eax
  272ac0:	cmp    $0x2,%eax
  272ac3:	je     272c6c <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x64c>
  272ac9:	cmp    $0x3,%eax
  272acc:	je     273209 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xbe9>
  272ad2:	xor    %eax,%eax
  272ad4:	mov    %eax,%ecx
  272ad6:	and    $0x3,%ecx
  272ad9:	cmp    $0x1,%ecx
  272adc:	je     272c7e <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x65e>
  272ae2:	jmp    272cb9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x699>
  272ae7:	nopw   0x0(%rax,%rax,1)
  272af0:	add    %rbp,-0x18(%r14)
  272af4:	sub    %rbp,-0x20(%r14)
  272af8:	mov    0x10(%rsp),%rax
  272afd:	mov    0x60(%rsp),%rcx
  272b02:	cmp    0x4a0(%rax),%rcx
  272b09:	jae    272b80 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x560>
  272b0b:	mov    0x10(%rsp),%rax
  272b10:	mov    0x498(%rax),%rax
  272b17:	mov    0x58(%rsp),%rcx
  272b1c:	mov    0x28(%rsp),%rdx
  272b21:	cmp    %ecx,0xb0(%rax,%rdx,1)
  272b28:	jne    272b80 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x560>
  272b2a:	add    0x28(%rsp),%rax
  272b2f:	mov    0xa8(%rax),%rdi
  272b36:	test   %rdi,%rdi
  272b39:	je     272b80 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x560>
  272b3b:	mov    0x538(%rdi),%rax
  272b42:	cmp    %rax,%rbp
  272b45:	mov    %rax,%rsi
  272b48:	cmovb  %rbp,%rsi
  272b4c:	sub    %rsi,%rax
  272b4f:	mov    %rax,0x538(%rdi)
  272b56:	mov    0x88(%rsp),%rdx
  272b5e:	mov    0x20(%rsp),%rcx
  272b63:	mov    0x48(%rsp),%r8
  272b68:	call   1ba900 <zfstack::conn::Conn::after_app_read>
  272b6d:	mov    0x10(%rsp),%rdi
  272b72:	mov    0x50(%rsp),%rsi
  272b77:	call   1cc0e0 <zfstack::shard::Shard::after_conn_change>
  272b7c:	nopl   0x0(%rax)
  272b80:	test   %rbp,%rbp
  272b83:	jne    272b9c <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x57c>
  272b85:	jmp    272780 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x160>
  272b8a:	nopw   0x0(%rax,%rax,1)
  272b90:	mov    %r13,%rbp
  272b93:	test   %r13,%r13
  272b96:	je     272780 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x160>
  272b9c:	mov    -0x28(%r14),%rdx
  272ba0:	test   %rdx,%rdx
  272ba3:	je     274822 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2202>
  272ba9:	mov    -0x30(%r14),%rax
  272bad:	mov    -0x40(%r14),%rsi
  272bb1:	mov    -0x38(%r14),%r15
  272bb5:	cmp    %rsi,%rax
  272bb8:	mov    $0x0,%ecx
  272bbd:	cmovae %rsi,%rcx
  272bc1:	mov    %rax,%rdi
  272bc4:	sub    %rcx,%rdi
  272bc7:	shl    $0x5,%rdi
  272bcb:	mov    0x10(%r15,%rdi,1),%rcx
  272bd0:	mov    %rbp,%r13
  272bd3:	sub    %rcx,%r13
  272bd6:	jb     272cfe <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x6de>
  272bdc:	lea    0x1(%rax),%rcx
  272be0:	cmp    %rsi,%rcx
  272be3:	mov    $0x0,%ecx
  272be8:	cmovae %rsi,%rcx
  272bec:	neg    %rcx
  272bef:	add    %rax,%rcx
  272bf2:	inc    %rcx
  272bf5:	mov    %rcx,-0x30(%r14)
  272bf9:	dec    %rdx
  272bfc:	mov    %rdx,-0x28(%r14)
  272c00:	shl    $0x5,%rax
  272c04:	mov    (%r15,%rax,1),%rcx
  272c08:	test   %rcx,%rcx
  272c0b:	je     272b90 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x570>
  272c0d:	add    %rax,%r15
  272c10:	mov    0x18(%r15),%rdi
  272c14:	mov    0x8(%r15),%rsi
  272c18:	mov    0x10(%r15),%rdx
  272c1c:	call   *0x20(%rcx)
  272c1f:	jmp    272b90 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x570>
  272c24:	data16 data16 cs nopw 0x0(%rax,%rax,1)
  272c30:	mov    %rbp,%rdi
  272c33:	call   459e0 <std::io::error::Error::kind>
  272c38:	cmp    $0x23,%al
  272c3a:	jne    27314f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb2f>
  272c40:	mov    %ebp,%eax
  272c42:	and    $0x3,%eax
  272c45:	cmp    $0x1,%eax
  272c48:	jne    272780 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x160>
  272c4e:	mov    %rbp,%r13
  272c51:	dec    %r13
  272c54:	mov    -0x1(%rbp),%r15
  272c58:	mov    0x7(%rbp),%rbp
  272c5c:	mov    0x0(%rbp),%rax
  272c60:	test   %rax,%rax
  272c63:	je     272ce1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x6c1>
  272c65:	mov    %r15,%rdi
  272c68:	call   *%rax
  272c6a:	jmp    272ce1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x6c1>
  272c6c:	mov    0xb0(%rsp),%rax
  272c74:	mov    %eax,%ecx
  272c76:	and    $0x3,%ecx
  272c79:	cmp    $0x1,%ecx
  272c7c:	jne    272cb9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x699>
  272c7e:	lea    -0x1(%rax),%r13
  272c82:	mov    -0x1(%rax),%rbp
  272c86:	mov    0x7(%rax),%r15
  272c8a:	mov    (%r15),%rax
  272c8d:	test   %rax,%rax
  272c90:	je     272c97 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x677>
  272c92:	mov    %rbp,%rdi
  272c95:	call   *%rax
  272c97:	cmpq   $0x0,0x8(%r15)
  272c9c:	mov    0x24eec5(%rip),%r15        # 4c1b68 <free@GLIBC_2.2.5>
  272ca3:	je     272cab <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x68b>
  272ca5:	mov    %rbp,%rdi
  272ca8:	call   *%r15
  272cab:	mov    %r13,%rdi
  272cae:	call   *%r15
  272cb1:	mov    0x150(%rsp),%rbp
  272cb9:	mov    %ebp,%eax
  272cbb:	and    $0x3,%eax
  272cbe:	cmp    $0x1,%eax
  272cc1:	jne    272780 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x160>
  272cc7:	lea    -0x1(%rbp),%r13
  272ccb:	mov    -0x1(%rbp),%r15
  272ccf:	mov    0x7(%rbp),%rbp
  272cd3:	mov    0x0(%rbp),%rax
  272cd7:	test   %rax,%rax
  272cda:	je     272ce1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x6c1>
  272cdc:	mov    %r15,%rdi
  272cdf:	call   *%rax
  272ce1:	cmpq   $0x0,0x8(%rbp)
  272ce6:	mov    0x24ee7b(%rip),%rbp        # 4c1b68 <free@GLIBC_2.2.5>
  272ced:	je     272cf4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x6d4>
  272cef:	mov    %r15,%rdi
  272cf2:	call   *%rbp
  272cf4:	mov    %r13,%rdi
  272cf7:	call   *%rbp
  272cf9:	jmp    272780 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x160>
  272cfe:	add    %rdi,%r15
  272d01:	mov    %rbp,0x1c0(%rsp)
  272d09:	test   %rbp,%rbp
  272d0c:	je     272d4e <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x72e>
  272d0e:	cmp    %rcx,%rbp
  272d11:	ja     27487d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x225d>
  272d17:	mov    (%r15),%rax
  272d1a:	mov    0x8(%r15),%rdx
  272d1e:	lea    0x18(%r15),%rsi
  272d22:	lea    0xb0(%rsp),%rdi
  272d2a:	call   *(%rax)
  272d2c:	sub    %rbp,0x10(%r15)
  272d30:	add    %rbp,0x8(%r15)
  272d34:	mov    0xb0(%rsp),%rax
  272d3c:	mov    0xb8(%rsp),%rsi
  272d44:	mov    0xc8(%rsp),%rdi
  272d4c:	jmp    272d5d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x73d>
  272d4e:	mov    0x8(%r15),%rsi
  272d52:	lea    0x244bf7(%rip),%rax        # 4b7950 <__do_global_dtors_aux_fini_array_entry+0x24588>
  272d59:	xor    %edi,%edi
  272d5b:	xor    %ebp,%ebp
  272d5d:	mov    %rbp,%rdx
  272d60:	call   *0x20(%rax)
  272d63:	jmp    272780 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x160>
  272d68:	xor    %eax,%eax
  272d6a:	jmp    2733fc <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xddc>
  272d6f:	mov    %al,0x150(%rsp)
  272d76:	lea    0x150(%rsp),%rax
  272d7e:	mov    %rax,0xb0(%rsp)
  272d86:	lea    0xaf4e3(%rip),%rax        # 322270 <<zfstack::CloseReason as core::fmt::Debug>::fmt>
  272d8d:	mov    %rax,0xb8(%rsp)
  272d95:	lea    0x163cf4(%rip),%rax        # 3d6a90 <unicode_width::tables::EMOJI_MODIFIER_LEAF_4+0xe8>
  272d9c:	mov    %rax,0x230(%rsp)
  272da4:	movq   $0x1,0x238(%rsp)
  272db0:	movq   $0x0,0x250(%rsp)
  272dbc:	lea    0xb0(%rsp),%rax
  272dc4:	mov    %rax,0x240(%rsp)
  272dcc:	movq   $0x1,0x248(%rsp)
  272dd8:	lea    0x630(%rsp),%rdi
  272de0:	lea    0x230(%rsp),%rsi
  272de8:	call   324c40 <alloc::fmt::format::format_inner>
  272ded:	lea    0x630(%rsp),%rsi
  272df5:	mov    $0x3,%edi
  272dfa:	call   457f0 <std::io::error::Error::new>
  272dff:	mov    %rax,%r12
  272e02:	mov    0x18(%rsp),%rax
  272e07:	mov    %eax,%ecx
  272e09:	and    $0x3,%ecx
  272e0c:	cmp    $0x1,%ecx
  272e0f:	jne    272e43 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x823>
  272e11:	lea    -0x1(%rax),%r13
  272e15:	mov    -0x1(%rax),%rbp
  272e19:	mov    0x7(%rax),%rbx
  272e1d:	mov    (%rbx),%rax
  272e20:	test   %rax,%rax
  272e23:	je     272e2a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x80a>
  272e25:	mov    %rbp,%rdi
  272e28:	call   *%rax
  272e2a:	cmpq   $0x0,0x8(%rbx)
  272e2f:	je     272e3a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x81a>
  272e31:	mov    %rbp,%rdi
  272e34:	call   *0x24ed2e(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  272e3a:	mov    %r13,%rdi
  272e3d:	call   *0x24ed25(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  272e43:	mov    %r12,0x18(%rsp)
  272e48:	mov    $0x1,%ecx
  272e4d:	xor    %eax,%eax
  272e4f:	mov    0x8(%rsp),%rdx
  272e54:	lock cmpxchg %ecx,(%rdx)
  272e58:	jne    27319a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb7a>
  272e5e:	mov    0x24f323(%rip),%rax        # 4c2188 <std::panicking::panic_count::GLOBAL_PANIC_COUNT>
  272e65:	movabs $0x8000000000000001,%rcx
  272e6f:	add    $0xfffffffffffffffe,%rcx
  272e73:	mov    %rcx,(%rsp)
  272e77:	test   %rcx,%rax
  272e7a:	jne    2731a9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb89>
  272e80:	movl   $0x0,0x48(%rsp)
  272e88:	mov    0x8(%rsp),%rax
  272e8d:	movzbl 0x4(%rax),%eax
  272e91:	test   %al,%al
  272e93:	jne    2731c5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xba5>
  272e99:	cmpq   $0x0,0x18(%rsp)
  272e9f:	mov    0x8(%rsp),%rsi
  272ea4:	jne    273055 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xa35>
  272eaa:	lea    0x18(%rsi),%r12
  272eae:	xchg   %ax,%ax
  272eb0:	cmpq   $0x0,0x80(%rsi)
  272eb8:	je     273055 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xa35>
  272ebe:	cmpq   $0x0,0x48(%rsi)
  272ec3:	je     272f00 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x8e0>
  272ec5:	mov    0x40(%rsi),%rax
  272ec9:	mov    0x30(%rsi),%rcx
  272ecd:	cmp    %rcx,%rax
  272ed0:	mov    $0x0,%edx
  272ed5:	cmovae %rcx,%rdx
  272ed9:	mov    0x38(%rsi),%rcx
  272edd:	sub    %rdx,%rax
  272ee0:	lea    (%rax,%rax,4),%rdx
  272ee4:	shl    $0x4,%rdx
  272ee8:	mov    0x10(%rcx,%rdx,1),%rax
  272eed:	mov    0x48(%rcx,%rdx,1),%rdx
  272ef2:	mov    %rax,%r13
  272ef5:	sub    %rdx,%r13
  272ef8:	jae    272f03 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x8e3>
  272efa:	jmp    274833 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2213>
  272eff:	nop
  272f00:	xor    %r13d,%r13d
  272f03:	cmp    $0x10000,%r13
  272f0a:	mov    $0x10000,%eax
  272f0f:	cmovae %rax,%r13
  272f13:	mov    0x8(%rsp),%rsi
  272f18:	mov    0x160(%rsi),%rax
  272f1f:	mov    0x98(%rax),%rbp
  272f26:	cmpq   $0x0,0x48(%rsi)
  272f2b:	je     272f80 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x960>
  272f2d:	mov    0x40(%rsi),%rax
  272f31:	mov    0x30(%rsi),%rcx
  272f35:	cmp    %rcx,%rax
  272f38:	mov    $0x0,%edx
  272f3d:	cmovae %rcx,%rdx
  272f41:	mov    0x38(%rsi),%rcx
  272f45:	sub    %rdx,%rax
  272f48:	lea    (%rax,%rax,4),%rdi
  272f4c:	shl    $0x4,%rdi
  272f50:	mov    0x10(%rcx,%rdi,1),%rax
  272f55:	mov    0x48(%rcx,%rdi,1),%rdx
  272f5a:	mov    %rax,%rsi
  272f5d:	sub    %rdx,%rsi
  272f60:	jb     274833 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2213>
  272f66:	add    %rdi,%rcx
  272f69:	add    0x8(%rcx),%rdx
  272f6d:	cmp    %rsi,%r13
  272f70:	jbe    272f90 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x970>
  272f72:	jmp    27484d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x222d>
  272f77:	nopw   0x0(%rax,%rax,1)
  272f80:	mov    $0x1,%edx
  272f85:	xor    %esi,%esi
  272f87:	cmp    %rsi,%r13
  272f8a:	ja     27484d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x222d>
  272f90:	mov    0x10(%rsp),%rdi
  272f95:	mov    0x50(%rsp),%rsi
  272f9a:	mov    %r13,%rcx
  272f9d:	call   1dc8d0 <zfstack::shard::Shard::write>
  272fa2:	mov    %rax,%rbx
  272fa5:	mov    %rdx,%r15
  272fa8:	mov    0x10(%rsp),%rdi
  272fad:	mov    0x8(%rsp),%rsi
  272fb2:	mov    %rax,%rdx
  272fb5:	mov    %rbp,%rcx
  272fb8:	mov    %r13,%r8
  272fbb:	call   1fef20 <zfstack::tokio_adapter::track_tx_wait>
  272fc0:	test   %rbx,%rbx
  272fc3:	jne    272fe3 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x9c3>
  272fc5:	mov    %r12,%rdi
  272fc8:	mov    %r15,%rsi
  272fcb:	call   1fece0 <zfstack::tokio_adapter::TxQueue::consume>
  272fd0:	cmpq   $0x0,0x18(%rsp)
  272fd6:	mov    0x8(%rsp),%rsi
  272fdb:	je     272eb0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x890>
  272fe1:	jmp    273055 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xa35>
  272fe3:	dec    %rbx
  272fe6:	cmp    $0x3,%rbx
  272fea:	mov    0x8(%rsp),%rsi
  272fef:	jb     273055 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xa35>
  272ff1:	lea    0x16c137(%rip),%rsi        # 3df12f <unicode_width::tables::EMOJI_MODIFIER_LEAF_7+0x118b>
  272ff8:	mov    $0x29,%edx
  272ffd:	mov    $0x3,%edi
  273002:	call   458c0 <std::io::error::Error::new>
  273007:	mov    %rax,%r12
  27300a:	mov    0x18(%rsp),%rax
  27300f:	mov    %eax,%ecx
  273011:	and    $0x3,%ecx
  273014:	cmp    $0x1,%ecx
  273017:	jne    27304b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xa2b>
  273019:	lea    -0x1(%rax),%r13
  27301d:	mov    -0x1(%rax),%r15
  273021:	mov    0x7(%rax),%rbx
  273025:	mov    (%rbx),%rax
  273028:	test   %rax,%rax
  27302b:	je     273032 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xa12>
  27302d:	mov    %r15,%rdi
  273030:	call   *%rax
  273032:	cmpq   $0x0,0x8(%rbx)
  273037:	je     273042 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xa22>
  273039:	mov    %r15,%rdi
  27303c:	call   *0x24eb26(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  273042:	mov    %r13,%rdi
  273045:	call   *0x24eb1d(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  27304b:	mov    %r12,0x18(%rsp)
  273050:	mov    0x8(%rsp),%rsi
  273055:	mov    0x80(%rsi),%rbx
  27305c:	mov    0x10(%rsp),%rax
  273061:	mov    0x60(%rsp),%rcx
  273066:	cmp    0x4a0(%rax),%rcx
  27306d:	jae    27327f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc5f>
  273073:	mov    0x498(%rax),%rax
  27307a:	mov    0x58(%rsp),%rcx
  27307f:	mov    0x28(%rsp),%rdx
  273084:	cmp    %ecx,0xb0(%rax,%rdx,1)
  27308b:	jne    27327f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc5f>
  273091:	add    %rdx,%rax
  273094:	mov    0xa8(%rax),%rax
  27309b:	test   %rax,%rax
  27309e:	je     27327f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc5f>
  2730a4:	xor    %edx,%edx
  2730a6:	cmpb   $0x9,0x69b(%rax)
  2730ad:	cmovne %ebx,%edx
  2730b0:	cmp    %edx,0x680(%rax)
  2730b6:	je     273264 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc44>
  2730bc:	mov    %edx,0x680(%rax)
  2730c2:	mov    0x1c0(%rax),%rcx
  2730c9:	mov    %edx,%edx
  2730cb:	neg    %rdx
  2730ce:	cmp    %rdx,%rcx
  2730d1:	setne  %sil
  2730d5:	cmp    0x698(%rax),%sil
  2730dc:	je     273264 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc44>
  2730e2:	cmp    %rdx,%rcx
  2730e5:	setne  0x698(%rax)
  2730ec:	mov    0x450(%rax),%rsi
  2730f3:	je     27325a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc3a>
  2730f9:	mov    0x20(%rsp),%rdi
  2730fe:	call   1ab600 <zfstack::budget::Budget::sender_add>
  273103:	jmp    273264 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc44>
  273108:	cmpb   $0x0,-0x8(%r14)
  27310d:	je     272e48 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x828>
  273113:	cmpb   $0x0,-0x7(%r14)
  273118:	jne    272e48 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x828>
  27311e:	mov    -0x58(%r14),%edi
  273122:	call   3c0f20 <tokio::net::tcp::stream::TcpStream::shutdown_std>
  273127:	mov    %rax,%r15
  27312a:	test   %rax,%rax
  27312d:	je     273250 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc30>
  273133:	cmpq   $0x0,0x18(%rsp)
  273139:	je     273145 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb25>
  27313b:	lea    0x18(%rsp),%rdi
  273140:	call   3b8e40 <core::ptr::drop_in_place<std::io::error::Error>>
  273145:	mov    %r15,0x18(%rsp)
  27314a:	jmp    272e48 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x828>
  27314f:	mov    0x18(%rsp),%rax
  273154:	mov    %eax,%ecx
  273156:	and    $0x3,%ecx
  273159:	cmp    $0x1,%ecx
  27315c:	jne    273190 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb70>
  27315e:	lea    -0x1(%rax),%r12
  273162:	mov    -0x1(%rax),%r15
  273166:	mov    0x7(%rax),%rbx
  27316a:	mov    (%rbx),%rax
  27316d:	test   %rax,%rax
  273170:	je     273177 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb57>
  273172:	mov    %r15,%rdi
  273175:	call   *%rax
  273177:	cmpq   $0x0,0x8(%rbx)
  27317c:	je     273187 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb67>
  27317e:	mov    %r15,%rdi
  273181:	call   *0x24e9e1(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  273187:	mov    %r12,%rdi
  27318a:	call   *0x24e9d8(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  273190:	mov    %rbp,0x18(%rsp)
  273195:	jmp    272e48 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x828>
  27319a:	mov    0x8(%rsp),%rdi
  27319f:	call   3dd50 <std::sys::sync::mutex::futex::Mutex::lock_contended>
  2731a4:	jmp    272e5e <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x83e>
  2731a9:	call   3de50 <std::panicking::panic_count::is_zero_slow_path>
  2731ae:	xor    $0x1,%al
  2731b0:	mov    %eax,0x48(%rsp)
  2731b4:	mov    0x8(%rsp),%rax
  2731b9:	movzbl 0x4(%rax),%eax
  2731bd:	test   %al,%al
  2731bf:	je     272e99 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x879>
  2731c5:	mov    0x8(%rsp),%rax
  2731ca:	mov    %rax,0x230(%rsp)
  2731d2:	mov    0x48(%rsp),%eax
  2731d6:	mov    %al,0x238(%rsp)
  2731dd:	lea    0x1afb08(%rip),%rdi        # 422cec <core::unicode::unicode_data::white_space::WHITESPACE_MAP+0x3c5f>
  2731e4:	lea    0x220a8d(%rip),%rcx        # 493c78 <__do_global_dtors_aux_fini_array_entry+0x8b0>
  2731eb:	lea    0x22679e(%rip),%r8        # 499990 <__do_global_dtors_aux_fini_array_entry+0x65c8>
  2731f2:	lea    0x230(%rsp),%rdx
  2731fa:	mov    $0x2b,%esi
  2731ff:	call   3d4f0 <core::result::unwrap_failed>
  273204:	jmp    274a86 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2466>
  273209:	mov    %ebp,%eax
  27320b:	and    $0x3,%eax
  27320e:	cmp    $0x1,%eax
  273211:	jne    272e48 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x828>
  273217:	mov    %rbp,%r12
  27321a:	dec    %r12
  27321d:	mov    -0x1(%rbp),%r15
  273221:	mov    0x7(%rbp),%rbx
  273225:	mov    (%rbx),%rax
  273228:	test   %rax,%rax
  27322b:	je     273232 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc12>
  27322d:	mov    %r15,%rdi
  273230:	call   *%rax
  273232:	cmpq   $0x0,0x8(%rbx)
  273237:	je     273242 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc22>
  273239:	mov    %r15,%rdi
  27323c:	call   *0x24e926(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  273242:	mov    %r12,%rdi
  273245:	call   *0x24e91d(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  27324b:	jmp    272e48 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x828>
  273250:	movb   $0x1,-0x7(%r14)
  273255:	jmp    272e48 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x828>
  27325a:	mov    0x20(%rsp),%rdi
  27325f:	call   1ab7b0 <zfstack::budget::Budget::sender_del>
  273264:	mov    0x10(%rsp),%rdi
  273269:	mov    0x50(%rsp),%rsi
  27326e:	call   1cc0e0 <zfstack::shard::Shard::after_conn_change>
  273273:	mov    0x8(%rsp),%rax
  273278:	mov    0x80(%rax),%rbx
  27327f:	cmpb   $0x0,0x48(%rsp)
  273284:	jne    273297 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc77>
  273286:	mov    0x24eefb(%rip),%rax        # 4c2188 <std::panicking::panic_count::GLOBAL_PANIC_COUNT>
  27328d:	test   %rax,(%rsp)
  273291:	jne    274774 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2154>
  273297:	xor    %eax,%eax
  273299:	mov    0x8(%rsp),%rcx
  27329e:	xchg   %eax,(%rcx)
  2732a0:	cmp    $0x2,%eax
  2732a3:	jne    2732c1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xca1>
  2732a5:	mov    $0xca,%edi
  2732aa:	mov    0x8(%rsp),%rsi
  2732af:	mov    $0x81,%edx
  2732b4:	mov    $0x1,%ecx
  2732b9:	xor    %eax,%eax
  2732bb:	call   *0x24eac7(%rip)        # 4c1d88 <syscall@GLIBC_2.2.5>
  2732c1:	mov    0x18(%rsp),%r12
  2732c6:	or     %r12,%rbx
  2732c9:	jne    2733ba <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd9a>
  2732cf:	cmpb   $0x0,-0x6(%r14)
  2732d4:	jne    2733b5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd95>
  2732da:	mov    -0x60(%r14),%rsi
  2732de:	xor    %r15d,%r15d
  2732e1:	lea    0x230(%rsp),%rdi
  2732e9:	mov    0xa0(%rsp),%rdx
  2732f1:	xor    %ecx,%ecx
  2732f3:	call   3bf530 <tokio::runtime::io::registration::Registration::poll_ready>
  2732f8:	movzbl 0x239(%rsp),%eax
  273300:	cmp    $0x2,%eax
  273303:	je     27340e <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xdee>
  273309:	cmp    $0x3,%eax
  27330c:	jne    27341d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xdfd>
  273312:	mov    0x8(%rsp),%rax
  273317:	movzbl 0x219(%rax),%eax
  27331e:	test   %al,%al
  273320:	je     273352 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd32>
  273322:	xor    %eax,%eax
  273324:	mov    0x8(%rsp),%rcx
  273329:	xchg   %al,0x219(%rcx)
  27332f:	test   %al,%al
  273331:	je     273352 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd32>
  273333:	mov    0x8(%rsp),%rax
  273338:	mov    0x160(%rax),%rdi
  27333f:	mov    0x1d0(%rax),%rsi
  273346:	add    $0x10,%rdi
  27334a:	xor    %r15d,%r15d
  27334d:	call   1a70b0 <zfstack::budget::GlobalBudget::remove_waiter>
  273352:	mov    0x10(%rsp),%rcx
  273357:	mov    0x498(%rcx),%rax
  27335e:	mov    0x60(%rsp),%rdx
  273363:	cmp    0x4a0(%rcx),%rdx
  27336a:	setae  %cl
  27336d:	test   %rax,%rax
  273370:	sete   %dl
  273373:	or     %cl,%dl
  273375:	jne    2733b0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd90>
  273377:	add    0x28(%rsp),%rax
  27337c:	mov    0x58(%rsp),%rcx
  273381:	cmp    %ecx,0xb0(%rax)
  273387:	jne    2733b0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd90>
  273389:	mov    0xa8(%rax),%rax
  273390:	test   %rax,%rax
  273393:	je     2733b0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd90>
  273395:	movzbl 0x69b(%rax),%ecx
  27339c:	cmp    $0x5,%ecx
  27339f:	ja     2733b0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd90>
  2733a1:	mov    $0x26,%edx
  2733a6:	bt     %ecx,%edx
  2733a9:	jae    2733b0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd90>
  2733ab:	cmpl   $0x1,(%rax)
  2733ae:	jne    2733b5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd95>
  2733b0:	movb   $0x1,-0x6(%r14)
  2733b5:	mov    0x18(%rsp),%r12
  2733ba:	test   %r12,%r12
  2733bd:	je     2733d6 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xdb6>
  2733bf:	mov    $0x1,%r15b
  2733c2:	mov    0x10(%rsp),%rdi
  2733c7:	mov    0x50(%rsp),%rsi
  2733cc:	mov    %r12,%rdx
  2733cf:	call   275f40 <zfstack::tokio_adapter::Driver<E,I,F>::finish_relay>
  2733d4:	jmp    2733fa <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xdda>
  2733d6:	movzbl -0x6(%r14),%eax
  2733db:	test   $0x1,%al
  2733dd:	je     2733fa <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xdda>
  2733df:	cmpb   $0x1,-0x7(%r14)
  2733e4:	jne    2733fa <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xdda>
  2733e6:	xor    %r15d,%r15d
  2733e9:	mov    0x10(%rsp),%rdi
  2733ee:	mov    0x50(%rsp),%rsi
  2733f3:	xor    %edx,%edx
  2733f5:	call   275f40 <zfstack::tokio_adapter::Driver<E,I,F>::finish_relay>
  2733fa:	mov    $0x1,%al
  2733fc:	add    $0x648,%rsp
  273403:	pop    %rbx
  273404:	pop    %r12
  273406:	pop    %r13
  273408:	pop    %r14
  27340a:	pop    %r15
  27340c:	pop    %rbp
  27340d:	ret
  27340e:	mov    0x230(%rsp),%r12
  273416:	mov    %r12,0x18(%rsp)
  27341b:	jmp    2733bf <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd9f>
  27341d:	mov    0x10(%rsp),%rax
  273422:	add    $0x558,%rax
  273428:	mov    %rax,0x98(%rsp)
  273430:	lea    0x223c11(%rip),%rax        # 497048 <__do_global_dtors_aux_fini_array_entry+0x3c80>
  273437:	mov    %rax,0x78(%rsp)
  27343c:	lea    0x223d7d(%rip),%rax        # 4971c0 <__do_global_dtors_aux_fini_array_entry+0x3df8>
  273443:	mov    %rax,0x148(%rsp)
  27344b:	lea    0x221446(%rip),%rax        # 494898 <__do_global_dtors_aux_fini_array_entry+0x14d0>
  273452:	mov    %rax,0x140(%rsp)
  27345a:	lea    0x165caf(%rip),%rbp        # 3d9110 <unicode_width::tables::EMOJI_MODIFIER_LEAF_4+0x2768>
  273461:	mov    0x28(%rsp),%rbx
  273466:	jmp    27347b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xe5b>
  273468:	movb   $0x1,-0x6(%r14)
  27346d:	mov    0x18(%rsp),%r12
  273472:	test   %r12,%r12
  273475:	jne    2733bf <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd9f>
  27347b:	movzbl -0x6(%r14),%eax
  273480:	test   %al,%al
  273482:	jne    2733db <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xdbb>
  273488:	mov    0x8(%rsp),%rax
  27348d:	mov    0x160(%rax),%rax
  273494:	mov    0x98(%rax),%rax
  27349b:	mov    %rax,0x48(%rsp)
  2734a0:	mov    0x10(%rsp),%rax
  2734a5:	mov    0x4a0(%rax),%rax
  2734ac:	cmp    %rax,0x60(%rsp)
  2734b1:	jae    273660 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1040>
  2734b7:	mov    0x10(%rsp),%rcx
  2734bc:	mov    0x498(%rcx),%rcx
  2734c3:	mov    0x58(%rsp),%rdx
  2734c8:	cmp    %edx,0xb0(%rcx,%rbx,1)
  2734cf:	jne    273660 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1040>
  2734d5:	add    %rbx,%rcx
  2734d8:	mov    0xa8(%rcx),%rdi
  2734df:	test   %rdi,%rdi
  2734e2:	je     273660 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1040>
  2734e8:	movzbl 0x69b(%rdi),%eax
  2734ef:	mov    $0x4,%r15d
  2734f5:	cmp    $0x5,%eax
  2734f8:	ja     27372a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x110a>
  2734fe:	mov    $0x26,%ecx
  273503:	bt     %eax,%ecx
  273506:	jae    27372a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x110a>
  27350c:	testb  $0x1,(%rdi)
  27350f:	jne    27372a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x110a>
  273515:	mov    %rdi,(%rsp)
  273519:	mov    0x88(%rsp),%rsi
  273521:	mov    0x20(%rsp),%rdx
  273526:	call   1b9fb0 <zfstack::conn::Conn::send_space>
  27352b:	mov    %rax,0x80(%rsp)
  273533:	mov    0x80(%rsp),%rax
  27353b:	cmp    $0x40000,%rax
  273541:	mov    $0x40000,%edx
  273546:	cmovb  %rax,%rdx
  27354a:	test   %rax,%rax
  27354d:	je     2736c8 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x10a8>
  273553:	mov    (%rsp),%rax
  273557:	mov    0x450(%rax),%rsi
  27355e:	mov    0x20(%rsp),%rdi
  273563:	mov    %rdx,0x38(%rsp)
  273568:	call   1ab8e0 <zfstack::budget::Budget::try_reserve>
  27356d:	test   %al,%al
  27356f:	je     273719 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x10f9>
  273575:	mov    (%rsp),%rax
  273579:	mov    0x450(%rax),%rbx
  273580:	lea    0x188(%rax),%r13
  273587:	cmpb   $0x2,0x1b2(%rax)
  27358e:	mov    0x180(%rax),%rax
  273595:	jne    2739be <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x139e>
  27359b:	cmp    $0x1,%rax
  27359f:	mov    (%rsp),%rdi
  2735a3:	jne    2735f2 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xfd2>
  2735a5:	mov    0x178(%rdi),%rcx
  2735ac:	mov    0x168(%rdi),%rdx
  2735b3:	mov    0x170(%rdi),%rax
  2735ba:	cmp    %rdx,%rcx
  2735bd:	mov    $0x0,%esi
  2735c2:	cmovb  %rsi,%rdx
  2735c6:	sub    %rdx,%rcx
  2735c9:	lea    (%rcx,%rcx,8),%rcx
  2735cd:	cmpq   $0x10000,0x8(%rax,%rcx,8)
  2735d6:	jne    2735f2 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xfd2>
  2735d8:	mov    0x1c0(%rdi),%rdx
  2735df:	mov    $0x10000,%esi
  2735e4:	sub    %rdx,%rsi
  2735e7:	cmp    0x38(%rsp),%rsi
  2735ec:	jae    273d12 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x16f2>
  2735f2:	add    $0x168,%rdi
  2735f9:	mov    %rdi,0x70(%rsp)
  2735fe:	call   1af660 <zfstack::buf::TxBuf::unwrap_for_growth>
  273603:	mov    (%rsp),%rdi
  273607:	mov    0x180(%rdi),%r12
  27360e:	test   %r12,%r12
  273611:	je     273a48 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1428>
  273617:	mov    0x178(%rdi),%rax
  27361e:	mov    0x168(%rdi),%rcx
  273625:	mov    0x170(%rdi),%rdx
  27362c:	cmp    %rcx,%rax
  27362f:	mov    $0x0,%esi
  273634:	cmovae %rcx,%rsi
  273638:	sub    %rsi,%rax
  27363b:	lea    (%rax,%rax,8),%rax
  27363f:	mov    0x8(%rdx,%rax,8),%rax
  273644:	mov    %r12,%rcx
  273647:	shl    $0x10,%rcx
  27364b:	lea    (%rcx,%rax,1),%r15
  27364f:	add    $0xffffffffffff0000,%r15
  273656:	jmp    273a4b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x142b>
  27365b:	nopl   0x0(%rax,%rax,1)
  273660:	movq   $0x4,0x1a0(%rsp)
  27366c:	xor    %ebx,%ebx
  27366e:	cmp    %rax,0x60(%rsp)
  273673:	jae    273770 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1150>
  273679:	mov    0x10(%rsp),%rax
  27367e:	mov    0x498(%rax),%rax
  273685:	mov    0x58(%rsp),%rcx
  27368a:	mov    0x28(%rsp),%rdx
  27368f:	cmp    %ecx,0xb0(%rax,%rdx,1)
  273696:	jne    273770 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1150>
  27369c:	add    0x28(%rsp),%rax
  2736a1:	cmpq   $0x0,0xa8(%rax)
  2736a9:	je     273770 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1150>
  2736af:	mov    0x10(%rsp),%rdi
  2736b4:	mov    0x50(%rsp),%rsi
  2736b9:	mov    $0x1,%edx
  2736be:	call   1cb970 <zfstack::shard::Shard::sync_share_waiter>
  2736c3:	jmp    273770 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1150>
  2736c8:	mov    (%rsp),%r15
  2736cc:	movb   $0x1,0x696(%r15)
  2736d4:	mov    0x450(%r15),%rsi
  2736db:	mov    0x20(%rsp),%rdi
  2736e0:	call   1ab470 <zfstack::budget::Budget::send_share>
  2736e5:	mov    %rax,%rbx
  2736e8:	mov    %r15,%rdi
  2736eb:	mov    0x88(%rsp),%rsi
  2736f3:	call   1b9d50 <zfstack::conn::Conn::sndbuf_limit>
  2736f8:	mov    $0x1,%r15d
  2736fe:	cmp    %rax,%rbx
  273701:	jae    27372a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x110a>
  273703:	mov    0x10(%rsp),%rax
  273708:	mov    0x768(%rax),%rax
  27370f:	lock incq 0x98(%rax)
  273717:	jmp    273724 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1104>
  273719:	mov    (%rsp),%rax
  27371d:	movb   $0x1,0x696(%rax)
  273724:	mov    $0x2,%r15d
  27372a:	mov    %r15,0x130(%rsp)
  273732:	xor    %ebx,%ebx
  273734:	mov    0x10(%rsp),%r13
  273739:	mov    %r13,%rdi
  27373c:	mov    0x50(%rsp),%rsi
  273741:	call   1cc0e0 <zfstack::shard::Shard::after_conn_change>
  273746:	mov    %r15,0x1a0(%rsp)
  27374e:	mov    %r12,0x1a8(%rsp)
  273756:	cmp    $0x5,%r15
  27375a:	sete   %bl
  27375d:	mov    0x4a0(%r13),%rax
  273764:	cmp    %rax,0x60(%rsp)
  273769:	jb     273679 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1059>
  27376f:	nop
  273770:	movdqu 0x1a0(%rsp),%xmm0
  273779:	movdqa %xmm0,0x100(%rsp)
  273782:	mov    0x100(%rsp),%r15
  27378a:	cmp    $0x5,%r15
  27378e:	jne    2737e0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x11c0>
  273790:	mov    0x8(%rsp),%rax
  273795:	movzbl 0x219(%rax),%eax
  27379c:	test   %al,%al
  27379e:	mov    0x28(%rsp),%rbx
  2737a3:	je     2737d2 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x11b2>
  2737a5:	xor    %eax,%eax
  2737a7:	mov    0x8(%rsp),%rcx
  2737ac:	xchg   %al,0x219(%rcx)
  2737b2:	test   %al,%al
  2737b4:	je     2737d2 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x11b2>
  2737b6:	mov    0x8(%rsp),%rax
  2737bb:	mov    0x160(%rax),%rdi
  2737c2:	mov    0x1d0(%rax),%rsi
  2737c9:	add    $0x10,%rdi
  2737cd:	call   1a70b0 <zfstack::budget::GlobalBudget::remove_waiter>
  2737d2:	mov    0x100(%rsp),%r15
  2737da:	jmp    273802 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x11e2>
  2737dc:	nopl   0x0(%rax)
  2737e0:	mov    $0x40000,%r8d
  2737e6:	mov    0x10(%rsp),%rdi
  2737eb:	mov    0x8(%rsp),%rsi
  2737f0:	mov    %r15,%rdx
  2737f3:	mov    0x48(%rsp),%rcx
  2737f8:	call   1fef20 <zfstack::tokio_adapter::track_tx_wait>
  2737fd:	mov    0x28(%rsp),%rbx
  273802:	movslq 0x0(%rbp,%r15,4),%rax
  273807:	add    %rbp,%rax
  27380a:	jmp    *%rax
  27380c:	mov    0x108(%rsp),%rax
  273814:	test   %rax,%rax
  273817:	je     273934 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1314>
  27381d:	add    %rax,-0x10(%r14)
  273821:	jmp    27346d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xe4d>
  273826:	mov    0x108(%rsp),%r12
  27382e:	mov    %r12,%rdi
  273831:	call   459e0 <std::io::error::Error::kind>
  273836:	cmp    $0xd,%al
  273838:	jne    2738d7 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x12b7>
  27383e:	mov    %r12,0x230(%rsp)
  273846:	mov    -0x60(%r14),%rdi
  27384a:	mov    0xa0(%rsp),%rsi
  273852:	call   3c1390 <tokio::net::tcp::stream::TcpStream::poll_read_ready>
  273857:	test   %rax,%rax
  27385a:	jne    2747d9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x21b9>
  273860:	mov    %edx,%eax
  273862:	and    $0x3,%eax
  273865:	cmp    $0x1,%eax
  273868:	jne    2738aa <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x128a>
  27386a:	mov    %rdx,%r12
  27386d:	dec    %r12
  273870:	mov    -0x1(%rdx),%r15
  273874:	mov    0x7(%rdx),%rbx
  273878:	mov    (%rbx),%rax
  27387b:	test   %rax,%rax
  27387e:	je     273885 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1265>
  273880:	mov    %r15,%rdi
  273883:	call   *%rax
  273885:	cmpq   $0x0,0x8(%rbx)
  27388a:	mov    0x24e2d7(%rip),%rbx        # 4c1b68 <free@GLIBC_2.2.5>
  273891:	je     273898 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1278>
  273893:	mov    %r15,%rdi
  273896:	call   *%rbx
  273898:	mov    %r12,%rdi
  27389b:	call   *%rbx
  27389d:	mov    0x230(%rsp),%r12
  2738a5:	mov    0x28(%rsp),%rbx
  2738aa:	mov    %r12d,%eax
  2738ad:	and    $0x3,%eax
  2738b0:	cmp    $0x1,%eax
  2738b3:	jne    27346d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xe4d>
  2738b9:	lea    -0x1(%r12),%r13
  2738be:	mov    -0x1(%r12),%r15
  2738c3:	mov    0x7(%r12),%rbx
  2738c8:	mov    (%rbx),%rax
  2738cb:	test   %rax,%rax
  2738ce:	je     273912 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x12f2>
  2738d0:	mov    %r15,%rdi
  2738d3:	call   *%rax
  2738d5:	jmp    273912 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x12f2>
  2738d7:	mov    %r12,%rdi
  2738da:	call   459e0 <std::io::error::Error::kind>
  2738df:	cmp    $0x23,%al
  2738e1:	jne    27478f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x216f>
  2738e7:	mov    %r12d,%eax
  2738ea:	and    $0x3,%eax
  2738ed:	cmp    $0x1,%eax
  2738f0:	jne    27346d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xe4d>
  2738f6:	lea    -0x1(%r12),%r13
  2738fb:	mov    -0x1(%r12),%r15
  273900:	mov    0x7(%r12),%rbx
  273905:	mov    (%rbx),%rax
  273908:	test   %rax,%rax
  27390b:	je     273912 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x12f2>
  27390d:	mov    %r15,%rdi
  273910:	call   *%rax
  273912:	cmpq   $0x0,0x8(%rbx)
  273917:	mov    0x24e24a(%rip),%rbx        # 4c1b68 <free@GLIBC_2.2.5>
  27391e:	je     273925 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1305>
  273920:	mov    %r15,%rdi
  273923:	call   *%rbx
  273925:	mov    %r13,%rdi
  273928:	call   *%rbx
  27392a:	mov    0x28(%rsp),%rbx
  27392f:	jmp    27346d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xe4d>
  273934:	movb   $0x1,-0x6(%r14)
  273939:	mov    0x10(%rsp),%rax
  27393e:	mov    0x60(%rsp),%rcx
  273943:	cmp    0x4a0(%rax),%rcx
  27394a:	jae    27346d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xe4d>
  273950:	mov    0x10(%rsp),%rax
  273955:	mov    0x498(%rax),%rax
  27395c:	mov    0x58(%rsp),%rcx
  273961:	cmp    %ecx,0xb0(%rax,%rbx,1)
  273968:	jne    27346d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xe4d>
  27396e:	add    %rbx,%rax
  273971:	mov    0xa8(%rax),%rax
  273978:	test   %rax,%rax
  27397b:	je     27346d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xe4d>
  273981:	testb  $0x1,(%rax)
  273984:	jne    273a34 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1414>
  27398a:	movzbl 0x69b(%rax),%ecx
  273991:	movzbl %cl,%edx
  273994:	lea    -0x1(%rdx),%esi
  273997:	cmp    $0x2,%esi
  27399a:	jae    273a0e <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x13ee>
  27399c:	mov    0x1c0(%rax),%rdx
  2739a3:	add    0x490(%rax),%rdx
  2739aa:	movq   $0x1,(%rax)
  2739b1:	mov    %rdx,0x8(%rax)
  2739b5:	mov    $0x3,%dl
  2739b7:	cmp    $0x2,%cl
  2739ba:	je     273a2e <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x140e>
  2739bc:	jmp    273a34 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1414>
  2739be:	mov    (%rsp),%r15
  2739c2:	mov    0x1a0(%r15),%rcx
  2739c9:	mov    0x10(%rsp),%rdx
  2739ce:	cmp    0x6f8(%rdx),%rcx
  2739d5:	jne    273c61 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1641>
  2739db:	mov    0x700(%rdx),%rcx
  2739e2:	test   %rax,%rax
  2739e5:	sete   %sil
  2739e9:	mov    0x1a8(%r15),%rdx
  2739f0:	cmp    %rcx,%rdx
  2739f3:	setne  %dil
  2739f7:	or     %sil,%dil
  2739fa:	je     273c4a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x162a>
  273a00:	cmp    %rcx,%rdx
  273a03:	jne    273c5d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x163d>
  273a09:	jmp    27359b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xf7b>
  273a0e:	cmp    $0x5,%edx
  273a11:	jne    273a34 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1414>
  273a13:	mov    0x1c0(%rax),%rcx
  273a1a:	add    0x490(%rax),%rcx
  273a21:	movq   $0x1,(%rax)
  273a28:	mov    %rcx,0x8(%rax)
  273a2c:	mov    $0x7,%dl
  273a2e:	mov    %dl,0x69b(%rax)
  273a34:	mov    0x10(%rsp),%rdi
  273a39:	mov    0x50(%rsp),%rsi
  273a3e:	call   1cc0e0 <zfstack::shard::Shard::after_conn_change>
  273a43:	jmp    27346d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xe4d>
  273a48:	xor    %r15d,%r15d
  273a4b:	mov    0x1c0(%rdi),%rsi
  273a52:	add    0x1b8(%rdi),%rsi
  273a59:	mov    0x80(%rsp),%rcx
  273a61:	cmp    $0x10000,%rcx
  273a68:	mov    $0x10000,%eax
  273a6d:	cmovb  %rcx,%rax
  273a71:	mov    %r15,%rcx
  273a74:	mov    %rsi,0x40(%rsp)
  273a79:	sub    %rsi,%rcx
  273a7c:	mov    $0x0,%edx
  273a81:	cmovae %rcx,%rdx
  273a85:	mov    %r12,0x90(%rsp)
  273a8d:	cmp    %rax,%rdx
  273a90:	jae    273b26 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1506>
  273a96:	mov    0x90(%rsp),%rcx
  273a9e:	test   %rcx,%rcx
  273aa1:	mov    $0x10000,%r12d
  273aa7:	mov    $0x800,%eax
  273aac:	cmove  %rax,%r12
  273ab0:	cmpq   $0x801,0x80(%rsp)
  273abc:	mov    $0x10000,%eax
  273ac1:	cmovae %rax,%r12
  273ac5:	cmp    $0xffffffffffffffff,%rcx
  273ac9:	je     273b07 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x14e7>
  273acb:	lea    0x1(%rcx),%rdx
  273acf:	mov    0x70(%rsp),%rax
  273ad4:	mov    (%rax),%rsi
  273ad7:	lea    0x230(%rsp),%rdi
  273adf:	mov    0x20(%rsp),%rcx
  273ae4:	mov    %rbx,%r8
  273ae7:	call   1b0b50 <zfstack::buf::TxBuf::prepare_index>
  273aec:	mov    0x230(%rsp),%rdi
  273af4:	movabs $0x8000000000000001,%rcx
  273afe:	cmp    %rcx,%rdi
  273b01:	jne    273c92 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1672>
  273b07:	xor    %eax,%eax
  273b09:	mov    (%rsp),%rdx
  273b0d:	cmp    0x40(%rsp),%r15
  273b12:	setbe  %cl
  273b15:	xor    $0x1,%al
  273b17:	test   %al,%cl
  273b19:	jne    273c33 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1613>
  273b1f:	mov    0x180(%rdx),%r12
  273b26:	test   %r12,%r12
  273b29:	je     274938 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2318>
  273b2f:	mov    (%rsp),%rdx
  273b33:	cmpb   $0x2,0x1b2(%rdx)
  273b3a:	mov    0x38(%rsp),%r15
  273b3f:	je     274929 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2309>
  273b45:	movzbl 0x1b0(%rdx),%eax
  273b4c:	movb   $0x0,0x1b0(%rdx)
  273b53:	cmp    $0x1,%al
  273b55:	jne    273b74 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1554>
  273b57:	mov    0x190(%rdx),%rax
  273b5e:	mov    0x1a0(%rdx),%rcx
  273b65:	lock sub %rax,0x88(%rcx)
  273b6d:	mov    0x180(%rdx),%r12
  273b74:	cmp    $0x1,%r12
  273b78:	je     273b9a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x157a>
  273b7a:	test   %r12,%r12
  273b7d:	je     274938 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2318>
  273b83:	mov    0x178(%rdx),%rax
  273b8a:	mov    0x168(%rdx),%rcx
  273b91:	mov    0x170(%rdx),%rbx
  273b98:	jmp    273bdf <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x15bf>
  273b9a:	mov    0x178(%rdx),%rax
  273ba1:	mov    0x168(%rdx),%rcx
  273ba8:	mov    0x170(%rdx),%rbx
  273baf:	cmp    %rcx,%rax
  273bb2:	mov    $0x0,%edx
  273bb7:	cmovae %rcx,%rdx
  273bbb:	mov    %rax,%rsi
  273bbe:	sub    %rdx,%rsi
  273bc1:	lea    (%rsi,%rsi,8),%rdx
  273bc5:	cmpq   $0x10000,0x8(%rbx,%rdx,8)
  273bce:	jne    273bdf <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x15bf>
  273bd0:	mov    0x40(%rsp),%rdx
  273bd5:	movzwl %dx,%edx
  273bd8:	mov    %rdx,0x40(%rsp)
  273bdd:	jmp    273c01 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x15e1>
  273bdf:	cmp    %rcx,%rax
  273be2:	mov    $0x0,%edx
  273be7:	cmovae %rcx,%rdx
  273beb:	mov    %rax,%rsi
  273bee:	sub    %rdx,%rsi
  273bf1:	lea    (%rsi,%rsi,8),%rsi
  273bf5:	mov    0x40(%rsp),%rdx
  273bfa:	sub    0x8(%rbx,%rsi,8),%rdx
  273bff:	jae    273c08 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x15e8>
  273c01:	xor    %edi,%edi
  273c03:	jmp    273df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x17d4>
  273c08:	mov    %rdx,%rsi
  273c0b:	shr    $0x10,%rsi
  273c0f:	lea    0x1(%rsi),%rdi
  273c13:	cmp    %rsi,%r12
  273c16:	jbe    2749ae <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x238e>
  273c1c:	sub    %rdi,%r12
  273c1f:	jne    273dec <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x17cc>
  273c25:	mov    %rbx,%rcx
  273c28:	mov    %rbx,%rdx
  273c2b:	mov    %rbx,%r8
  273c2e:	jmp    273e4d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x182d>
  273c33:	mov    0x70(%rsp),%rdi
  273c38:	call   1b0a50 <zfstack::buf::TxBuf::trim_empty_index>
  273c3d:	mov    (%rsp),%r15
  273c41:	mov    0x450(%r15),%rbx
  273c48:	jmp    273c61 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1641>
  273c4a:	mov    0x0(%r13),%rcx
  273c4e:	test   %rcx,%rcx
  273c51:	je     273c5d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x163d>
  273c53:	cmp    %rbx,0x10(%rcx)
  273c57:	je     27359b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xf7b>
  273c5d:	mov    (%rsp),%r15
  273c61:	mov    0x20(%rsp),%rdi
  273c66:	mov    %rbx,%rsi
  273c69:	mov    0x38(%rsp),%rdx
  273c6e:	call   1abba0 <zfstack::budget::Budget::cancel_reserve>
  273c73:	movb   $0x1,0x696(%r15)
  273c7b:	movq   $0x3,0x130(%rsp)
  273c87:	mov    $0x3,%r15d
  273c8d:	jmp    273732 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1112>
  273c92:	lea    0x238(%rsp),%rdx
  273c9a:	mov    0x40(%rdx),%rax
  273c9e:	lea    0xb8(%rsp),%rsi
  273ca6:	mov    %rax,0x40(%rsi)
  273caa:	movdqu (%rdx),%xmm0
  273cae:	movdqu 0x10(%rdx),%xmm1
  273cb3:	movdqu 0x20(%rdx),%xmm2
  273cb8:	movdqu 0x30(%rdx),%xmm3
  273cbd:	movdqu %xmm3,0x30(%rsi)
  273cc2:	movdqu %xmm2,0x20(%rsi)
  273cc7:	movdqu %xmm1,0x10(%rsi)
  273ccc:	movdqu %xmm0,(%rsi)
  273cd0:	mov    %rdi,0xb0(%rsp)
  273cd8:	lea    -0x1(%rcx),%rax
  273cdc:	mov    %rdi,0x68(%rsp)
  273ce1:	mov    %rax,0x30(%rsp)
  273ce6:	cmp    %rax,%rdi
  273ce9:	jne    274240 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1c20>
  273cef:	mov    (%rsp),%rax
  273cf3:	cmpb   $0x2,0x1b2(%rax)
  273cfa:	jne    274314 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1cf4>
  273d00:	pxor   %xmm0,%xmm0
  273d04:	movdqa %xmm0,0x230(%rsp)
  273d0d:	jmp    27434a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1d2a>
  273d12:	add    0x1b8(%rdi),%edx
  273d18:	movzwl %dx,%ebx
  273d1b:	mov    $0x10000,%r8d
  273d21:	mov    $0x10000,%edx
  273d26:	sub    %rbx,%rdx
  273d29:	mov    0x38(%rsp),%rsi
  273d2e:	cmp    %rdx,%rsi
  273d31:	mov    %rdx,%r12
  273d34:	cmovb  %rsi,%r12
  273d38:	lea    (%r12,%rbx,1),%r15
  273d3c:	cmp    %rsi,%rdx
  273d3f:	cmovb  %r8,%r15
  273d43:	lea    (%rax,%rcx,8),%r13
  273d47:	mov    0x10(%rax,%rcx,8),%rdi
  273d4c:	mov    %r15,%rdx
  273d4f:	sub    %rdi,%rdx
  273d52:	jbe    273d75 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1755>
  273d54:	cmp    $0x10000,%r15
  273d5b:	ja     274a1e <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23fe>
  273d61:	add    0x0(%r13),%rdi
  273d65:	xor    %esi,%esi
  273d67:	call   *0x24df7b(%rip)        # 4c1ce8 <memset@GLIBC_2.2.5>
  273d6d:	mov    %r15,0x10(%r13)
  273d71:	mov    0x8(%r13),%r8
  273d75:	cmp    %r8,%r15
  273d78:	ja     2749d4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23b4>
  273d7e:	sub    %rbx,%r15
  273d81:	cmp    %r15,%r12
  273d84:	ja     2749e5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23c5>
  273d8a:	mov    0x38(%rsp),%rax
  273d8f:	sub    %r12,%rax
  273d92:	cmp    %rbx,%rax
  273d95:	ja     2749f3 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23d3>
  273d9b:	mov    0x0(%r13),%rcx
  273d9f:	add    %rcx,%rbx
  273da2:	mov    %rbx,0x230(%rsp)
  273daa:	mov    %r12,0x238(%rsp)
  273db2:	mov    %rcx,0x240(%rsp)
  273dba:	mov    %rax,0x248(%rsp)
  273dc2:	mov    0xa8(%rsp),%rdi
  273dca:	lea    0x230(%rsp),%rsi
  273dd2:	call   3c0950 <tokio::net::tcp::stream::TcpStream::try_read_vectored>
  273dd7:	mov    %rdx,%r12
  273dda:	test   %rax,%rax
  273ddd:	je     274251 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1c31>
  273de3:	mov    (%rsp),%r15
  273de7:	jmp    274209 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1be9>
  273dec:	movzwl %dx,%edx
  273def:	mov    %rdx,0x40(%rsp)
  273df4:	add    %rdi,%rax
  273df7:	cmp    %rcx,%rax
  273dfa:	mov    %rcx,%rdx
  273dfd:	mov    $0x0,%r9d
  273e03:	cmovb  %r9,%rdx
  273e07:	sub    %rdx,%rax
  273e0a:	mov    %rcx,%rsi
  273e0d:	sub    %rax,%rsi
  273e10:	lea    (%rax,%r12,1),%rdi
  273e14:	cmp    %r12,%rsi
  273e17:	cmovb  %rcx,%rdi
  273e1b:	mov    %r12,%r8
  273e1e:	sub    %rsi,%r8
  273e21:	cmovb  %r9,%r8
  273e25:	lea    (%rax,%rax,8),%rcx
  273e29:	lea    (%rbx,%rcx,8),%rcx
  273e2d:	lea    (%rdi,%rdi,8),%rdx
  273e31:	lea    (%rbx,%rdx,8),%rdx
  273e35:	lea    (%r8,%r8,8),%r8
  273e39:	lea    (%rbx,%r8,8),%r8
  273e3d:	cmp    %rdi,%rax
  273e40:	jne    273e60 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1840>
  273e42:	mov    %rcx,%r9
  273e45:	mov    %rdx,%r10
  273e48:	cmp    %r12,%rsi
  273e4b:	jb     273e6c <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x184c>
  273e4d:	mov    $0x1,%esi
  273e52:	mov    %rdx,%r10
  273e55:	mov    %rcx,%r9
  273e58:	xor    %r13d,%r13d
  273e5b:	jmp    273f0e <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x18ee>
  273e60:	mov    %rbx,%r9
  273e63:	mov    %r8,%r10
  273e66:	mov    %rdx,%r8
  273e69:	mov    %rcx,%rbx
  273e6c:	mov    0x8(%rbx),%rcx
  273e70:	mov    %rcx,%r13
  273e73:	mov    0x40(%rsp),%rax
  273e78:	sub    %rax,%r13
  273e7b:	cmp    %r15,%r13
  273e7e:	cmovae %r15,%r13
  273e82:	mov    0x10(%rbx),%rdi
  273e86:	lea    (%rax,%r13,1),%r12
  273e8a:	mov    %r12,%rdx
  273e8d:	sub    %rdi,%rdx
  273e90:	jbe    273eee <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x18ce>
  273e92:	mov    %r10,0x1b0(%rsp)
  273e9a:	mov    %r9,0x1b8(%rsp)
  273ea2:	mov    %rcx,0x30(%rsp)
  273ea7:	cmp    %rcx,%r12
  273eaa:	ja     274a07 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23e7>
  273eb0:	mov    %r8,%r15
  273eb3:	mov    (%rbx),%rax
  273eb6:	mov    %rax,0x68(%rsp)
  273ebb:	add    %rax,%rdi
  273ebe:	xor    %esi,%esi
  273ec0:	call   *0x24de22(%rip)        # 4c1ce8 <memset@GLIBC_2.2.5>
  273ec6:	mov    0x68(%rsp),%rsi
  273ecb:	mov    %r12,0x10(%rbx)
  273ecf:	mov    %r15,%r8
  273ed2:	mov    0x38(%rsp),%r15
  273ed7:	mov    0x30(%rsp),%rcx
  273edc:	mov    0x1b8(%rsp),%r9
  273ee4:	mov    0x1b0(%rsp),%r10
  273eec:	jmp    273ef1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x18d1>
  273eee:	mov    (%rbx),%rsi
  273ef1:	cmp    0x40(%rsp),%r12
  273ef6:	jb     27499a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x237a>
  273efc:	cmp    %rcx,%r12
  273eff:	ja     2749c6 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23a6>
  273f05:	add    $0x48,%rbx
  273f09:	add    0x40(%rsp),%rsi
  273f0e:	cmp    %r8,%rbx
  273f11:	jne    273f1b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x18fb>
  273f13:	mov    %r9,%rbx
  273f16:	cmp    %r10,%r9
  273f19:	je     273f53 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1933>
  273f1b:	mov    0x8(%rbx),%rax
  273f1f:	sub    %r13,%r15
  273f22:	cmp    %r15,%rax
  273f25:	cmovb  %rax,%r15
  273f29:	mov    (%rbx),%r12
  273f2c:	mov    0x10(%rbx),%rdi
  273f30:	mov    %r15,%rdx
  273f33:	sub    %rdi,%rdx
  273f36:	jbe    273f5c <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x193c>
  273f38:	add    %r12,%rdi
  273f3b:	mov    %rsi,0x68(%rsp)
  273f40:	xor    %esi,%esi
  273f42:	call   *0x24dda0(%rip)        # 4c1ce8 <memset@GLIBC_2.2.5>
  273f48:	mov    0x68(%rsp),%rsi
  273f4d:	mov    %r15,0x10(%rbx)
  273f51:	jmp    273f5c <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x193c>
  273f53:	mov    $0x1,%r12d
  273f59:	xor    %r15d,%r15d
  273f5c:	mov    %rsi,0x230(%rsp)
  273f64:	mov    %r13,0x238(%rsp)
  273f6c:	mov    %r12,0x240(%rsp)
  273f74:	mov    %r15,0x248(%rsp)
  273f7c:	mov    0xa8(%rsp),%rdi
  273f84:	lea    0x230(%rsp),%rsi
  273f8c:	call   3c0950 <tokio::net::tcp::stream::TcpStream::try_read_vectored>
  273f91:	mov    %rax,%rbx
  273f94:	mov    %rdx,%r12
  273f97:	mov    %rax,0xb0(%rsp)
  273f9f:	mov    %rdx,0xb8(%rsp)
  273fa7:	test   %rax,%rax
  273faa:	mov    %rdx,%rcx
  273fad:	mov    $0x0,%eax
  273fb2:	cmovne %rax,%rcx
  273fb6:	add    %r13,%r15
  273fb9:	cmp    %r15,%rcx
  273fbc:	ja     27494d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x232d>
  273fc2:	mov    (%rsp),%r15
  273fc6:	add    0x1c0(%r15),%rcx
  273fcd:	mov    %rcx,0x1c0(%r15)
  273fd4:	mov    0x180(%r15),%rax
  273fdb:	mov    0x90(%rsp),%r13
  273fe3:	cmp    %r13,%rax
  273fe6:	jbe    274122 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1b02>
  273fec:	mov    0x1c0(%r15),%rcx
  273ff3:	mov    0x1b8(%r15),%rdi
  273ffa:	add    %rcx,%rdi
  273ffd:	mov    0x178(%r15),%r8
  274004:	mov    0x168(%r15),%rsi
  27400b:	mov    0x170(%r15),%rdx
  274012:	cmp    %rsi,%r8
  274015:	mov    %rsi,%r9
  274018:	mov    $0x0,%r11d
  27401e:	cmovb  %r11,%r9
  274022:	lea    (%r8,%rax,1),%r10
  274026:	dec    %r10
  274029:	sub    %r9,%r8
  27402c:	lea    (%r8,%r8,8),%r8
  274030:	mov    %rax,%r9
  274033:	shl    $0x10,%r9
  274037:	add    0x8(%rdx,%r8,8),%r9
  27403c:	cmp    %rsi,%r10
  27403f:	mov    %rsi,%r8
  274042:	cmovb  %r11,%r8
  274046:	sub    %r8,%r10
  274049:	lea    (%r10,%r10,8),%r8
  27404d:	sub    0x8(%rdx,%r8,8),%r9
  274052:	add    $0xffffffffffff0000,%r9
  274059:	cmp    %r9,%rdi
  27405c:	ja     274122 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1b02>
  274062:	dec    %rax
  274065:	mov    %rax,0x180(%r15)
  27406c:	add    0x178(%r15),%rax
  274073:	cmp    %rsi,%rax
  274076:	mov    $0x0,%ecx
  27407b:	cmovae %rsi,%rcx
  27407f:	sub    %rcx,%rax
  274082:	lea    (%rax,%rax,8),%rcx
  274086:	movzbl 0x42(%rdx,%rcx,8),%eax
  27408b:	cmp    $0x2,%al
  27408d:	je     274918 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x22f8>
  274093:	lea    (%rdx,%rcx,8),%rcx
  274097:	movzwl 0x40(%rcx),%edx
  27409b:	mov    %dx,0x270(%rsp)
  2740a3:	movdqu (%rcx),%xmm0
  2740a7:	movdqu 0x10(%rcx),%xmm1
  2740ac:	movdqu 0x20(%rcx),%xmm2
  2740b1:	movdqu 0x30(%rcx),%xmm3
  2740b6:	movdqa %xmm3,0x260(%rsp)
  2740bf:	movdqa %xmm2,0x250(%rsp)
  2740c8:	movdqa %xmm1,0x240(%rsp)
  2740d1:	movdqa %xmm0,0x230(%rsp)
  2740da:	mov    %al,0x272(%rsp)
  2740e1:	movzbl 0x47(%rcx),%eax
  2740e5:	lea    0x238(%rsp),%rdx
  2740ed:	mov    %al,0x3f(%rdx)
  2740f0:	mov    0x43(%rcx),%eax
  2740f3:	mov    %eax,0x3b(%rdx)
  2740f6:	mov    0x98(%rsp),%rdi
  2740fe:	lea    0x230(%rsp),%rsi
  274106:	call   1aea40 <zfstack::buf::BlockPool::put>
  27410b:	mov    0x180(%r15),%rax
  274112:	cmp    %r13,%rax
  274115:	ja     273fec <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x19cc>
  27411b:	mov    0x1c0(%r15),%rcx
  274122:	test   %rcx,%rcx
  274125:	jne    274204 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1be4>
  27412b:	jmp    2741ab <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1b8b>
  27412d:	lea    (%rcx,%rdx,8),%rcx
  274131:	movzwl 0x40(%rcx),%edx
  274135:	mov    %dx,0x220(%rsp)
  27413d:	movdqu (%rcx),%xmm0
  274141:	movdqu 0x10(%rcx),%xmm1
  274146:	movdqu 0x20(%rcx),%xmm2
  27414b:	movdqu 0x30(%rcx),%xmm3
  274150:	movdqa %xmm3,0x210(%rsp)
  274159:	movdqa %xmm2,0x200(%rsp)
  274162:	movdqa %xmm1,0x1f0(%rsp)
  27416b:	movdqa %xmm0,0x1e0(%rsp)
  274174:	mov    %al,0x222(%rsp)
  27417b:	movzbl 0x47(%rcx),%eax
  27417f:	lea    0x223(%rsp),%rdx
  274187:	mov    %al,0x4(%rdx)
  27418a:	mov    0x43(%rcx),%eax
  27418d:	mov    %eax,(%rdx)
  27418f:	mov    0x98(%rsp),%rdi
  274197:	lea    0x1e0(%rsp),%rsi
  27419f:	call   1aea40 <zfstack::buf::BlockPool::put>
  2741a4:	mov    0x180(%r15),%rax
  2741ab:	test   %rax,%rax
  2741ae:	je     2741ef <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1bcf>
  2741b0:	dec    %rax
  2741b3:	mov    %rax,0x180(%r15)
  2741ba:	add    0x178(%r15),%rax
  2741c1:	mov    0x168(%r15),%rdx
  2741c8:	mov    0x170(%r15),%rcx
  2741cf:	cmp    %rdx,%rax
  2741d2:	mov    $0x0,%esi
  2741d7:	cmovae %rdx,%rsi
  2741db:	sub    %rsi,%rax
  2741de:	lea    (%rax,%rax,8),%rdx
  2741e2:	movzbl 0x42(%rcx,%rdx,8),%eax
  2741e7:	cmp    $0x2,%al
  2741e9:	jne    27412d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1b0d>
  2741ef:	movq   $0x0,0x1b8(%r15)
  2741fa:	mov    0x70(%rsp),%rdi
  2741ff:	call   1b0a50 <zfstack::buf::TxBuf::trim_empty_index>
  274204:	test   $0x1,%bl
  274207:	je     274267 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1c47>
  274209:	mov    0x450(%r15),%rsi
  274210:	mov    0x20(%rsp),%rdi
  274215:	mov    0x38(%rsp),%rdx
  27421a:	call   1abba0 <zfstack::budget::Budget::cancel_reserve>
  27421f:	mov    %r12,0x138(%rsp)
  274227:	movq   $0x5,0x130(%rsp)
  274233:	mov    $0x1,%bl
  274235:	mov    $0x5,%r15d
  27423b:	jmp    273734 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1114>
  274240:	movq   $0x0,0x110(%rsp)
  27424c:	jmp    27436d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1d4d>
  274251:	cmp    %r12,0x38(%rsp)
  274256:	mov    (%rsp),%r15
  27425a:	jb     274a2d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x240d>
  274260:	add    %r12,0x1c0(%r15)
  274267:	mov    0x38(%rsp),%rdx
  27426c:	sub    %r12,%rdx
  27426f:	jbe    274282 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1c62>
  274271:	mov    0x450(%r15),%rsi
  274278:	mov    0x20(%rsp),%rdi
  27427d:	call   1abba0 <zfstack::budget::Budget::cancel_reserve>
  274282:	add    %r12,0x580(%r15)
  274289:	mov    0x1c0(%r15),%rax
  274290:	mov    0x680(%r15),%ecx
  274297:	neg    %rcx
  27429a:	cmp    %rcx,%rax
  27429d:	setne  %dl
  2742a0:	cmp    0x698(%r15),%dl
  2742a7:	je     2742d6 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1cb6>
  2742a9:	cmp    %rcx,%rax
  2742ac:	mov    (%rsp),%rax
  2742b0:	setne  0x698(%rax)
  2742b7:	mov    0x450(%rax),%rsi
  2742be:	je     2742cc <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1cac>
  2742c0:	mov    0x20(%rsp),%rdi
  2742c5:	call   1ab600 <zfstack::budget::Budget::sender_add>
  2742ca:	jmp    2742d6 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1cb6>
  2742cc:	mov    0x20(%rsp),%rdi
  2742d1:	call   1ab7b0 <zfstack::budget::Budget::sender_del>
  2742d6:	cmpq   $0x3ffff,0x80(%rsp)
  2742e2:	ja     2742f6 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1cd6>
  2742e4:	cmp    %r12,0x38(%rsp)
  2742e9:	jne    2742f6 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1cd6>
  2742eb:	mov    (%rsp),%rax
  2742ef:	movb   $0x1,0x696(%rax)
  2742f6:	movq   $0x0,0x130(%rsp)
  274302:	mov    %r12,0x138(%rsp)
  27430a:	xor    %r15d,%r15d
  27430d:	xor    %ebx,%ebx
  27430f:	jmp    273734 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1114>
  274314:	xor    %r8d,%r8d
  274317:	cmpq   $0x0,0x90(%rsp)
  274320:	sete   %r8b
  274324:	lea    0x230(%rsp),%rdi
  27432c:	mov    %r13,%rsi
  27432f:	mov    0x20(%rsp),%rdx
  274334:	mov    %rbx,%rcx
  274337:	call   1acea0 <zfstack::budget::MemoryLease::prepare_index_peer>
  27433c:	testb  $0x1,0x230(%rsp)
  274344:	jne    273b07 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x14e7>
  27434a:	lea    0x238(%rsp),%rax
  274352:	movdqu (%rax),%xmm0
  274356:	movdqu 0x10(%rax),%xmm1
  27435b:	movdqa %xmm1,0x120(%rsp)
  274364:	movdqa %xmm0,0x110(%rsp)
  27436d:	lea    0x230(%rsp),%rdi
  274375:	mov    0x98(%rsp),%rsi
  27437d:	mov    0x20(%rsp),%rdx
  274382:	mov    %rbx,%rcx
  274385:	mov    %r12,%r8
  274388:	call   1ae590 <zfstack::buf::BlockPool::get>
  27438d:	cmpb   $0x2,0x272(%rsp)
  274395:	jne    2743de <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1dbe>
  274397:	cmpq   $0x0,0x110(%rsp)
  2743a0:	je     2743af <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1d8f>
  2743a2:	lea    0x110(%rsp),%rdi
  2743aa:	call   131ac0 <core::ptr::drop_in_place<zfstack::budget::IndexPeerReservation>>
  2743af:	mov    0x30(%rsp),%rax
  2743b4:	cmp    %rax,0x68(%rsp)
  2743b9:	je     273b07 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x14e7>
  2743bf:	lea    0xb0(%rsp),%rdi
  2743c7:	call   1358b0 <core::ptr::drop_in_place<alloc::collections::vec_deque::VecDeque<zfstack::buf::Block>>>
  2743cc:	lea    0xd0(%rsp),%rdi
  2743d4:	call   12eea0 <core::ptr::drop_in_place<zfstack::budget::MemoryLease>>
  2743d9:	jmp    273b07 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x14e7>
  2743de:	mov    0x270(%rsp),%rax
  2743e6:	mov    %rax,0x190(%rsp)
  2743ee:	movdqu 0x230(%rsp),%xmm0
  2743f7:	movdqu 0x240(%rsp),%xmm1
  274400:	movdqu 0x250(%rsp),%xmm2
  274409:	movdqu 0x260(%rsp),%xmm3
  274412:	movdqa %xmm3,0x180(%rsp)
  27441b:	movdqa %xmm2,0x170(%rsp)
  274424:	movdqa %xmm1,0x160(%rsp)
  27442d:	movdqa %xmm0,0x150(%rsp)
  274436:	mov    0x110(%rsp),%r12
  27443e:	test   %r12,%r12
  274441:	je     2744b1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1e91>
  274443:	movdqa 0x110(%rsp),%xmm0
  27444c:	movdqa 0x120(%rsp),%xmm1
  274455:	movdqa %xmm1,0x1d0(%rsp)
  27445e:	movdqa %xmm0,0x1c0(%rsp)
  274467:	mov    (%rsp),%rax
  27446b:	cmpb   $0x2,0x1b2(%rax)
  274472:	je     274a7a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x245a>
  274478:	movdqa 0x110(%rsp),%xmm0
  274481:	movdqa 0x120(%rsp),%xmm1
  27448a:	movdqa %xmm1,0x240(%rsp)
  274493:	movdqa %xmm0,0x230(%rsp)
  27449c:	mov    %r13,%rdi
  27449f:	lea    0x230(%rsp),%rsi
  2744a7:	call   1ad3a0 <zfstack::budget::MemoryLease::commit_index_peer>
  2744ac:	xor    %r13d,%r13d
  2744af:	jmp    2744b4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1e94>
  2744b1:	mov    $0x1,%r13b
  2744b4:	mov    0x68(%rsp),%rsi
  2744b9:	cmp    0x30(%rsp),%rsi
  2744be:	jne    2745e9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1fc9>
  2744c4:	mov    0x190(%rsp),%rax
  2744cc:	mov    %rax,0x270(%rsp)
  2744d4:	movdqa 0x150(%rsp),%xmm0
  2744dd:	movdqa 0x160(%rsp),%xmm1
  2744e6:	movdqa 0x170(%rsp),%xmm2
  2744ef:	movdqa 0x180(%rsp),%xmm3
  2744f8:	movdqa %xmm3,0x260(%rsp)
  274501:	movdqa %xmm2,0x250(%rsp)
  27450a:	movdqa %xmm1,0x240(%rsp)
  274513:	movdqa %xmm0,0x230(%rsp)
  27451c:	mov    (%rsp),%rdi
  274520:	mov    0x168(%rdi),%rcx
  274527:	mov    0x180(%rdi),%rax
  27452e:	cmp    %rcx,%rax
  274531:	jne    27454f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1f2f>
  274533:	mov    0x70(%rsp),%rdi
  274538:	call   149ab0 <alloc::collections::vec_deque::VecDeque<T,A>::grow>
  27453d:	mov    (%rsp),%rdi
  274541:	mov    0x168(%rdi),%rcx
  274548:	mov    0x180(%rdi),%rax
  27454f:	mov    0x178(%rdi),%rdx
  274556:	add    %rax,%rdx
  274559:	cmp    %rcx,%rdx
  27455c:	mov    $0x0,%esi
  274561:	cmovae %rcx,%rsi
  274565:	sub    %rsi,%rdx
  274568:	inc    %rax
  27456b:	mov    0x170(%rdi),%rcx
  274572:	lea    (%rdx,%rdx,8),%rdx
  274576:	movdqa 0x230(%rsp),%xmm0
  27457f:	movdqa 0x240(%rsp),%xmm1
  274588:	movdqa 0x250(%rsp),%xmm2
  274591:	movdqa 0x260(%rsp),%xmm3
  27459a:	mov    0x270(%rsp),%rsi
  2745a2:	mov    %rsi,0x40(%rcx,%rdx,8)
  2745a7:	movdqu %xmm3,0x30(%rcx,%rdx,8)
  2745ad:	movdqu %xmm2,0x20(%rcx,%rdx,8)
  2745b3:	movdqu %xmm1,0x10(%rcx,%rdx,8)
  2745b9:	movdqu %xmm0,(%rcx,%rdx,8)
  2745be:	mov    %rax,0x180(%rdi)
  2745c5:	test   %r12,%r12
  2745c8:	je     27476d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x214d>
  2745ce:	test   %r13b,%r13b
  2745d1:	je     27476d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x214d>
  2745d7:	lea    0x110(%rsp),%rdi
  2745df:	call   131ac0 <core::ptr::drop_in_place<zfstack::budget::IndexPeerReservation>>
  2745e4:	jmp    27476d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x214d>
  2745e9:	mov    0x190(%rsp),%rax
  2745f1:	mov    %rax,0x270(%rsp)
  2745f9:	movdqa 0x150(%rsp),%xmm0
  274602:	movdqa 0x160(%rsp),%xmm1
  27460b:	movdqa 0x170(%rsp),%xmm2
  274614:	movdqa 0x180(%rsp),%xmm3
  27461d:	movdqa %xmm3,0x260(%rsp)
  274626:	movdqa %xmm2,0x250(%rsp)
  27462f:	movdqa %xmm1,0x240(%rsp)
  274638:	movdqa %xmm0,0x230(%rsp)
  274641:	mov    0xc8(%rsp),%rax
  274649:	cmp    %rsi,%rax
  27464c:	jne    27466b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x204b>
  27464e:	lea    0xb0(%rsp),%rdi
  274656:	call   149ab0 <alloc::collections::vec_deque::VecDeque<T,A>::grow>
  27465b:	mov    0xb0(%rsp),%rsi
  274663:	mov    0xc8(%rsp),%rax
  27466b:	mov    0xc0(%rsp),%rcx
  274673:	add    %rax,%rcx
  274676:	cmp    %rsi,%rcx
  274679:	mov    $0x0,%edx
  27467e:	cmovae %rsi,%rdx
  274682:	sub    %rdx,%rcx
  274685:	mov    0xb8(%rsp),%rdx
  27468d:	lea    (%rcx,%rcx,8),%rcx
  274691:	movaps 0x230(%rsp),%xmm0
  274699:	movaps 0x240(%rsp),%xmm1
  2746a1:	movaps 0x250(%rsp),%xmm2
  2746a9:	movaps 0x260(%rsp),%xmm3
  2746b1:	mov    0x270(%rsp),%rsi
  2746b9:	mov    %rsi,0x40(%rdx,%rcx,8)
  2746be:	movups %xmm3,0x30(%rdx,%rcx,8)
  2746c3:	movups %xmm2,0x20(%rdx,%rcx,8)
  2746c8:	movups %xmm1,0x10(%rdx,%rcx,8)
  2746cd:	movups %xmm0,(%rdx,%rcx,8)
  2746d1:	inc    %rax
  2746d4:	mov    %rax,0xc8(%rsp)
  2746dc:	movdqu 0xb0(%rsp),%xmm0
  2746e5:	movdqu 0xd0(%rsp),%xmm1
  2746ee:	movdqu 0xe0(%rsp),%xmm2
  2746f7:	movdqu 0xf0(%rsp),%xmm3
  274700:	movdqa %xmm3,0x270(%rsp)
  274709:	movdqa %xmm2,0x260(%rsp)
  274712:	movdqa %xmm1,0x250(%rsp)
  27471b:	mov    0xc0(%rsp),%rax
  274723:	mov    %rax,0x240(%rsp)
  27472b:	mov    0xc8(%rsp),%rax
  274733:	mov    %rax,0x248(%rsp)
  27473b:	movdqa %xmm0,0x230(%rsp)
  274744:	mov    0x70(%rsp),%rdi
  274749:	lea    0x230(%rsp),%rsi
  274751:	call   1b0d50 <zfstack::buf::TxBuf::commit_index>
  274756:	test   %r12,%r12
  274759:	je     27476d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x214d>
  27475b:	test   %r13b,%r13b
  27475e:	je     27476d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x214d>
  274760:	lea    0x110(%rsp),%rdi
  274768:	call   131ac0 <core::ptr::drop_in_place<zfstack::budget::IndexPeerReservation>>
  27476d:	mov    $0x1,%al
  27476f:	jmp    273b09 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x14e9>
  274774:	call   3de50 <std::panicking::panic_count::is_zero_slow_path>
  274779:	test   %al,%al
  27477b:	jne    273297 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc77>
  274781:	mov    0x8(%rsp),%rax
  274786:	movb   $0x1,0x4(%rax)
  27478a:	jmp    273297 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc77>
  27478f:	mov    0x18(%rsp),%rax
  274794:	mov    %eax,%ecx
  274796:	and    $0x3,%ecx
  274799:	cmp    $0x1,%ecx
  27479c:	jne    273416 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xdf6>
  2747a2:	lea    -0x1(%rax),%r15
  2747a6:	mov    -0x1(%rax),%r13
  2747aa:	mov    0x7(%rax),%rbx
  2747ae:	mov    (%rbx),%rax
  2747b1:	test   %rax,%rax
  2747b4:	je     2747bb <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x219b>
  2747b6:	mov    %r13,%rdi
  2747b9:	call   *%rax
  2747bb:	cmpq   $0x0,0x8(%rbx)
  2747c0:	je     2747cb <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x21ab>
  2747c2:	mov    %r13,%rdi
  2747c5:	call   *0x24d39d(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  2747cb:	mov    %r15,%rdi
  2747ce:	call   *0x24d394(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  2747d4:	jmp    273416 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xdf6>
  2747d9:	mov    %r12d,%eax
  2747dc:	and    $0x3,%eax
  2747df:	cmp    $0x1,%eax
  2747e2:	jne    2733b5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd95>
  2747e8:	lea    -0x1(%r12),%r15
  2747ed:	mov    -0x1(%r12),%r13
  2747f2:	mov    0x7(%r12),%rbx
  2747f7:	mov    (%rbx),%rax
  2747fa:	test   %rax,%rax
  2747fd:	je     274804 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x21e4>
  2747ff:	mov    %r13,%rdi
  274802:	call   *%rax
  274804:	cmpq   $0x0,0x8(%rbx)
  274809:	je     274814 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x21f4>
  27480b:	mov    %r13,%rdi
  27480e:	call   *0x24d354(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274814:	mov    %r15,%rdi
  274817:	call   *0x24d34b(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  27481d:	jmp    2733b5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd95>
  274822:	lea    0x22514f(%rip),%rdi        # 499978 <__do_global_dtors_aux_fini_array_entry+0x65b0>
  274829:	call   3d5e0 <core::option::unwrap_failed>
  27482e:	jmp    274a86 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2466>
  274833:	lea    0x224dce(%rip),%rcx        # 499608 <__do_global_dtors_aux_fini_array_entry+0x6240>
  27483a:	mov    %rdx,%rdi
  27483d:	mov    %rax,%rsi
  274840:	mov    %rcx,%rdx
  274843:	call   3d0e0 <core::slice::index::slice_start_index_len_fail>
  274848:	jmp    274a86 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2466>
  27484d:	lea    0x225154(%rip),%rdx        # 4999a8 <__do_global_dtors_aux_fini_array_entry+0x65e0>
  274854:	mov    %r13,%rdi
  274857:	call   3d1d0 <core::slice::index::slice_end_index_len_fail>
  27485c:	jmp    274a86 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2466>
  274861:	lea    0x2250f8(%rip),%rax        # 499960 <__do_global_dtors_aux_fini_array_entry+0x6598>
  274868:	mov    $0x40,%esi
  27486d:	mov    %rdx,%rdi
  274870:	mov    %rax,%rdx
  274873:	call   3d1d0 <core::slice::index::slice_end_index_len_fail>
  274878:	jmp    274a86 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2466>
  27487d:	mov    %rcx,0x1e0(%rsp)
  274885:	lea    0x1c0(%rsp),%rax
  27488d:	mov    %rax,0x150(%rsp)
  274895:	lea    -0x21974c(%rip),%rax        # 5b150 <core::fmt::num::<impl core::fmt::Debug for u64>::fmt>
  27489c:	mov    %rax,0x158(%rsp)
  2748a4:	lea    0x1e0(%rsp),%rcx
  2748ac:	mov    %rcx,0x160(%rsp)
  2748b4:	mov    %rax,0x168(%rsp)
  2748bc:	lea    0x2430b5(%rip),%rax        # 4b7978 <__do_global_dtors_aux_fini_array_entry+0x245b0>
  2748c3:	mov    %rax,0xb0(%rsp)
  2748cb:	movq   $0x2,0xb8(%rsp)
  2748d7:	movq   $0x0,0xd0(%rsp)
  2748e3:	lea    0x150(%rsp),%rax
  2748eb:	mov    %rax,0xc0(%rsp)
  2748f3:	movq   $0x2,0xc8(%rsp)
  2748ff:	lea    0x243092(%rip),%rsi        # 4b7998 <__do_global_dtors_aux_fini_array_entry+0x245d0>
  274906:	lea    0xb0(%rsp),%rdi
  27490e:	call   3d1b0 <core::panicking::panic_fmt>
  274913:	jmp    274a86 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2466>
  274918:	lea    0x222811(%rip),%rdi        # 497130 <__do_global_dtors_aux_fini_array_entry+0x3d68>
  27491f:	call   3d5e0 <core::option::unwrap_failed>
  274924:	jmp    274a86 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2466>
  274929:	lea    0x2227c0(%rip),%rax        # 4970f0 <__do_global_dtors_aux_fini_array_entry+0x3d28>
  274930:	mov    %rax,0x148(%rsp)
  274938:	xor    %r15d,%r15d
  27493b:	mov    0x148(%rsp),%rdi
  274943:	call   3d5e0 <core::option::unwrap_failed>
  274948:	jmp    274a86 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2466>
  27494d:	lea    0x2227b4(%rip),%rax        # 497108 <__do_global_dtors_aux_fini_array_entry+0x3d40>
  274954:	mov    %rax,0x230(%rsp)
  27495c:	movq   $0x1,0x238(%rsp)
  274968:	movq   $0x8,0x240(%rsp)
  274974:	pxor   %xmm0,%xmm0
  274978:	movdqu %xmm0,0x248(%rsp)
  274981:	lea    0x222790(%rip),%rsi        # 497118 <__do_global_dtors_aux_fini_array_entry+0x3d50>
  274988:	lea    0x230(%rsp),%rdi
  274990:	call   3d1b0 <core::panicking::panic_fmt>
  274995:	jmp    274a86 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2466>
  27499a:	mov    0x40(%rsp),%rdi
  27499f:	lea    0x2226ba(%rip),%rax        # 497060 <__do_global_dtors_aux_fini_array_entry+0x3c98>
  2749a6:	mov    %rax,0x140(%rsp)
  2749ae:	xor    %r15d,%r15d
  2749b1:	mov    %r12,%rsi
  2749b4:	mov    0x140(%rsp),%rdx
  2749bc:	call   3d570 <core::slice::index::slice_index_order_fail>
  2749c1:	jmp    274a86 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2466>
  2749c6:	mov    %rcx,0x30(%rsp)
  2749cb:	lea    0x22268e(%rip),%rax        # 497060 <__do_global_dtors_aux_fini_array_entry+0x3c98>
  2749d2:	jmp    274a02 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23e2>
  2749d4:	mov    %r8,0x30(%rsp)
  2749d9:	mov    %r15,%r12
  2749dc:	lea    0x22267d(%rip),%rax        # 497060 <__do_global_dtors_aux_fini_array_entry+0x3c98>
  2749e3:	jmp    274a02 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23e2>
  2749e5:	mov    %r15,0x30(%rsp)
  2749ea:	lea    0x222757(%rip),%rax        # 497148 <__do_global_dtors_aux_fini_array_entry+0x3d80>
  2749f1:	jmp    274a02 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23e2>
  2749f3:	mov    %rax,%r12
  2749f6:	mov    %rbx,0x30(%rsp)
  2749fb:	lea    0x22275e(%rip),%rax        # 497160 <__do_global_dtors_aux_fini_array_entry+0x3d98>
  274a02:	mov    %rax,0x78(%rsp)
  274a07:	xor    %r15d,%r15d
  274a0a:	mov    %r12,%rdi
  274a0d:	mov    0x30(%rsp),%rsi
  274a12:	mov    0x78(%rsp),%rdx
  274a17:	call   3d1d0 <core::slice::index::slice_end_index_len_fail>
  274a1c:	jmp    274a86 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2466>
  274a1e:	mov    $0x10000,%eax
  274a23:	mov    %rax,0x30(%rsp)
  274a28:	mov    %r15,%r12
  274a2b:	jmp    274a07 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23e7>
  274a2d:	lea    0x2226d4(%rip),%rax        # 497108 <__do_global_dtors_aux_fini_array_entry+0x3d40>
  274a34:	mov    %rax,0x230(%rsp)
  274a3c:	movq   $0x1,0x238(%rsp)
  274a48:	movq   $0x8,0x240(%rsp)
  274a54:	pxor   %xmm0,%xmm0
  274a58:	movdqu %xmm0,0x248(%rsp)
  274a61:	xor    %r15d,%r15d
  274a64:	lea    0x22270d(%rip),%rsi        # 497178 <__do_global_dtors_aux_fini_array_entry+0x3db0>
  274a6b:	lea    0x230(%rsp),%rdi
  274a73:	call   3d1b0 <core::panicking::panic_fmt>
  274a78:	jmp    274a86 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2466>
  274a7a:	lea    0x22270f(%rip),%rdi        # 497190 <__do_global_dtors_aux_fini_array_entry+0x3dc8>
  274a81:	call   3d5e0 <core::option::unwrap_failed>
  274a86:	ud2
  274a88:	mov    %rax,%r14
  274a8b:	lea    0x230(%rsp),%rdi
  274a93:	call   12c650 <core::ptr::drop_in_place<zfstack::buf::Block>>
  274a98:	jmp    274aaf <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x248f>
  274a9a:	call   3d7b9 <core::panicking::panic_in_cleanup>
  274a9f:	mov    %rax,%r14
  274aa2:	lea    0x230(%rsp),%rdi
  274aaa:	call   12c650 <core::ptr::drop_in_place<zfstack::buf::Block>>
  274aaf:	mov    $0x1,%bl
  274ab1:	jmp    274b14 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x24f4>
  274ab3:	call   3d7b9 <core::panicking::panic_in_cleanup>
  274ab8:	mov    %rax,%r14
  274abb:	xor    %ebx,%ebx
  274abd:	jmp    274b14 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x24f4>
  274abf:	mov    %rax,%r14
  274ac2:	lea    0xd0(%rsp),%rdi
  274aca:	call   12eea0 <core::ptr::drop_in_place<zfstack::budget::MemoryLease>>
  274acf:	jmp    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274ad4:	call   3d7b9 <core::panicking::panic_in_cleanup>
  274ad9:	mov    %rax,%r14
  274adc:	lea    0x1c0(%rsp),%rdi
  274ae4:	call   131ac0 <core::ptr::drop_in_place<zfstack::budget::IndexPeerReservation>>
  274ae9:	jmp    274aee <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x24ce>
  274aeb:	mov    %rax,%r14
  274aee:	lea    0x150(%rsp),%rdi
  274af6:	call   12c650 <core::ptr::drop_in_place<zfstack::buf::Block>>
  274afb:	jmp    274b33 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2513>
  274afd:	jmp    274aff <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x24df>
  274aff:	mov    %rax,%r14
  274b02:	jmp    274b33 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2513>
  274b04:	mov    %rax,%r14
  274b07:	mov    $0x1,%bl
  274b09:	mov    0x110(%rsp),%r12
  274b11:	mov    $0x1,%r13b
  274b14:	test   %r12,%r12
  274b17:	je     274b2b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x250b>
  274b19:	test   %r13b,%r13b
  274b1c:	je     274b2b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x250b>
  274b1e:	lea    0x110(%rsp),%rdi
  274b26:	call   131ac0 <core::ptr::drop_in_place<zfstack::budget::IndexPeerReservation>>
  274b2b:	test   %bl,%bl
  274b2d:	je     274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274b33:	lea    0xb0(%rsp),%rdi
  274b3b:	call   133800 <core::ptr::drop_in_place<core::option::Option<zfstack::buf::TxIndex>>>
  274b40:	jmp    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274b45:	mov    %rax,%r14
  274b48:	cmpq   $0x0,0x8(%rbx)
  274b4d:	je     274b58 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2538>
  274b4f:	mov    %r13,%rdi
  274b52:	call   *0x24d010(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274b58:	mov    %r15,%rdi
  274b5b:	call   *0x24d007(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274b61:	jmp    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274b66:	mov    %rax,%r14
  274b69:	cmpq   $0x0,0x8(%rbx)
  274b6e:	je     274b79 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2559>
  274b70:	mov    %r13,%rdi
  274b73:	call   *0x24cfef(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274b79:	mov    %r15,%rdi
  274b7c:	jmp    274cdb <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x26bb>
  274b81:	call   3d7b9 <core::panicking::panic_in_cleanup>
  274b86:	jmp    274c3f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x261f>
  274b8b:	mov    %rax,%r14
  274b8e:	cmpq   $0x0,0x8(%rbx)
  274b93:	je     274b9e <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x257e>
  274b95:	mov    %r15,%rdi
  274b98:	call   *0x24cfca(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274b9e:	mov    %r12,%rdi
  274ba1:	call   *0x24cfc1(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274ba7:	jmp    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274bac:	mov    %rax,%r14
  274baf:	cmpq   $0x0,0x8(%rbx)
  274bb4:	je     274bbf <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x259f>
  274bb6:	mov    %r15,%rdi
  274bb9:	call   *0x24cfa9(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274bbf:	mov    %r13,%rdi
  274bc2:	call   *0x24cfa0(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274bc8:	mov    %r12,0x18(%rsp)
  274bcd:	jmp    274ddc <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27bc>
  274bd2:	jmp    274c3f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x261f>
  274bd4:	mov    %rax,%r14
  274bd7:	cmpq   $0x0,0x8(%rbx)
  274bdc:	je     274be7 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x25c7>
  274bde:	mov    %r15,%rdi
  274be1:	call   *0x24cf81(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274be7:	mov    %r12,%rdi
  274bea:	call   *0x24cf78(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274bf0:	mov    %rbp,0x18(%rsp)
  274bf5:	xor    %r15d,%r15d
  274bf8:	jmp    274dfc <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27dc>
  274bfd:	mov    %rax,%r14
  274c00:	mov    %r15,0x18(%rsp)
  274c05:	jmp    274e07 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27e7>
  274c0a:	jmp    274c0c <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x25ec>
  274c0c:	mov    %rax,%r14
  274c0f:	cmpq   $0x0,0x8(%rbx)
  274c14:	jne    274d1f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x26ff>
  274c1a:	jmp    274d28 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2708>
  274c1f:	mov    %rax,%r14
  274c22:	cmpq   $0x0,0x8(%rbx)
  274c27:	je     274c32 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2612>
  274c29:	mov    %r15,%rdi
  274c2c:	call   *0x24cf36(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274c32:	mov    %r12,%rdi
  274c35:	call   *0x24cf2d(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274c3b:	jmp    274cb3 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2693>
  274c3d:	jmp    274c3f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x261f>
  274c3f:	mov    %rax,%r14
  274c42:	test   %rbx,%rbx
  274c45:	je     274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274c4b:	lea    0xb8(%rsp),%rdi
  274c53:	call   3b8e40 <core::ptr::drop_in_place<std::io::error::Error>>
  274c58:	jmp    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274c5d:	call   3d7b9 <core::panicking::panic_in_cleanup>
  274c62:	jmp    274dd9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27b9>
  274c67:	jmp    274df1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d1>
  274c6c:	mov    %rax,%r14
  274c6f:	test   %bl,%bl
  274c71:	je     274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274c77:	lea    0x1a8(%rsp),%rdi
  274c7f:	call   3b8e40 <core::ptr::drop_in_place<std::io::error::Error>>
  274c84:	jmp    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274c89:	call   3d7b9 <core::panicking::panic_in_cleanup>
  274c8e:	mov    %rax,%r14
  274c91:	test   %bl,%bl
  274c93:	je     274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274c99:	lea    0x138(%rsp),%rdi
  274ca1:	call   3b8e40 <core::ptr::drop_in_place<std::io::error::Error>>
  274ca6:	jmp    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274cab:	call   3d7b9 <core::panicking::panic_in_cleanup>
  274cb0:	mov    %rax,%r14
  274cb3:	lea    0x230(%rsp),%rdi
  274cbb:	call   3b8e40 <core::ptr::drop_in_place<std::io::error::Error>>
  274cc0:	jmp    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274cc5:	mov    %rax,%r14
  274cc8:	cmpq   $0x0,0x8(%rbx)
  274ccd:	je     274cd8 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x26b8>
  274ccf:	mov    %rbp,%rdi
  274cd2:	call   *0x24ce90(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274cd8:	mov    %r13,%rdi
  274cdb:	call   *0x24ce87(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274ce1:	mov    %r12,0x18(%rsp)
  274ce6:	jmp    274e07 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27e7>
  274ceb:	jmp    274df1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d1>
  274cf0:	mov    %rax,%r14
  274cf3:	cmpl   $0x5,0x100(%rsp)
  274cfb:	jne    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274d01:	lea    0x108(%rsp),%rdi
  274d09:	call   3b8e40 <core::ptr::drop_in_place<std::io::error::Error>>
  274d0e:	jmp    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274d13:	jmp    274d15 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x26f5>
  274d15:	mov    %rax,%r14
  274d18:	cmpq   $0x0,0x8(%rbp)
  274d1d:	je     274d28 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2708>
  274d1f:	mov    %r15,%rdi
  274d22:	call   *0x24ce40(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274d28:	mov    %r13,%rdi
  274d2b:	call   *0x24ce37(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274d31:	jmp    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274d36:	mov    %rax,%r14
  274d39:	cmpq   $0x0,0x8(%r15)
  274d3e:	je     274d49 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2729>
  274d40:	mov    %rbp,%rdi
  274d43:	call   *0x24ce1f(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274d49:	mov    %r13,%rdi
  274d4c:	call   *0x24ce16(%rip)        # 4c1b68 <free@GLIBC_2.2.5>
  274d52:	jmp    274d92 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2772>
  274d54:	jmp    274df1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d1>
  274d59:	mov    %rax,%r14
  274d5c:	lea    0x230(%rsp),%rdi
  274d64:	call   112770 <core::ptr::drop_in_place<std::sync::poison::PoisonError<std::sync::mutex::MutexGuard<zfstack::budget::KindCounts>>>>
  274d69:	jmp    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274d6e:	mov    %rax,%r14
  274d71:	jmp    274df7 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d7>
  274d76:	mov    %rax,%r14
  274d79:	mov    %r13,%rdi
  274d7c:	mov    %r12,%rsi
  274d7f:	mov    %rbp,%rdx
  274d82:	call   *0x20(%r15)
  274d86:	jmp    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274d88:	call   3d7b9 <core::panicking::panic_in_cleanup>
  274d8d:	jmp    274df1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d1>
  274d8f:	mov    %rax,%r14
  274d92:	lea    0x150(%rsp),%rdi
  274d9a:	call   3b8e40 <core::ptr::drop_in_place<std::io::error::Error>>
  274d9f:	jmp    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274da1:	jmp    274df1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d1>
  274da3:	mov    %rax,%r14
  274da6:	mov    0x230(%rsp),%rax
  274dae:	test   %rax,%rax
  274db1:	je     274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274db3:	mov    0x248(%rsp),%rdi
  274dbb:	mov    0x238(%rsp),%rsi
  274dc3:	mov    0x240(%rsp),%rdx
  274dcb:	call   *0x20(%rax)
  274dce:	jmp    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274dd0:	call   3d7b9 <core::panicking::panic_in_cleanup>
  274dd5:	jmp    274df1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d1>
  274dd7:	jmp    274dd9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27b9>
  274dd9:	mov    %rax,%r14
  274ddc:	movzbl 0x48(%rsp),%esi
  274de1:	mov    0x8(%rsp),%rdi
  274de6:	call   3bffb0 <core::ptr::drop_in_place<std::sync::mutex::MutexGuard<tokio::runtime::io::registration_set::Synced>>>
  274deb:	jmp    274df4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d4>
  274ded:	jmp    274df1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d1>
  274def:	jmp    274df1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27d1>
  274df1:	mov    %rax,%r14
  274df4:	xor    %r15d,%r15d
  274df7:	mov    0x18(%rsp),%rbp
  274dfc:	test   %rbp,%rbp
  274dff:	sete   %al
  274e02:	or     %r15b,%al
  274e05:	jne    274e11 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27f1>
  274e07:	lea    0x18(%rsp),%rdi
  274e0c:	call   3b8e40 <core::ptr::drop_in_place<std::io::error::Error>>
  274e11:	mov    %r14,%rdi
  274e14:	call   34030 <_Unwind_Resume@plt>
  274e19:	call   3d7b9 <core::panicking::panic_in_cleanup>
  274e1e:	xchg   %ax,%ax

